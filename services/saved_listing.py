"""The Saved tab's reads: a filtered, sorted, paged list (songs saved on several services shown once) and
the detail of one item, each annotated with what the library already has."""

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models_saved import SavedBatch, SavedBatchItem, SavedEntry, SavedItem
from services import local_albums, saved_match
from services.album_merge import primary
from services.saved_store import KINDS, SOURCES, plain_text

SORTS = ("added", "added_asc", "artist", "title")
PER_PAGE = 100
PER_MAX = 200
SOURCE_ORDER = {"qobuz": 0, "spotify": 1, "youtube": 2}
MAX_TERMS = 8
ARTIST_DETAIL_MAX = 500
OPEN_BATCH_STATES = ("expanding", "active", "paused")


@dataclass(frozen=True)
class ListFilters:
    source: str = "all"
    kind: str = "track"
    missing: bool = False
    other_album: bool = False
    removed: bool = False
    q: str = ""
    sort: str = "added"
    page: int = 1
    per: int = PER_PAGE

    @classmethod
    def parse(cls, *, source: str = "all", kind: str = "track", missing: str | int | bool = 0,
              other_album: str | int | bool = 0, removed: str | int | bool = 0, q: str = "", sort: str = "added",
              page: int = 1, per: int = PER_PAGE) -> "ListFilters":
        """Unknown values fall back to the defaults instead of failing: they come from saved browser state."""
        def flag(value) -> bool:
            return str(value).strip().lower() in ("1", "true", "on", "yes")

        return cls(source=source if source in SOURCES else "all", kind=kind if kind in KINDS else "track",
                   missing=flag(missing), other_album=flag(other_album), removed=flag(removed), q=(q or "").strip()[:200],
                   sort=sort if sort in SORTS else "added", page=max(1, page), per=min(max(1, per), PER_MAX))

    def params(self) -> dict:
        return {k: int(v) if isinstance(v, bool) else v for k, v in asdict(self).items()}


def _terms(q: str) -> list[str]:
    return plain_text(q).split()[:MAX_TERMS]


def _conditions(f: ListFilters) -> list:
    conds = [SavedItem.kind == f.kind]
    if f.source != "all":
        conds.append(SavedItem.source == f.source)
    if not f.removed:
        conds.append(SavedItem.removed_at.is_(None))
    conds += [SavedItem.search_text.contains(term, autoescape=True) for term in _terms(f.q)]
    return conds


def _merged(f: ListFilters) -> bool:
    return f.source == "all"


def _grouped(f: ListFilters):
    group = SavedItem.group_key if _merged(f) else SavedItem.id
    added = func.coalesce(SavedItem.added_at, SavedItem.first_seen_at)
    rank = case(*((SavedItem.match_state == state, value) for state, value in saved_match.STATE_RANK.items()), else_=0)
    return (select(group.label("g"), func.max(added).label("added"), func.min(SavedItem.position).label("pos"),
                   func.min(func.lower(SavedItem.artist)).label("artist"), func.min(func.lower(SavedItem.title)).label("title"),
                   func.max(rank).label("rank"), func.max(SavedItem.matched_at).label("matched_at"))
            .where(*_conditions(f)).group_by(group).subquery())


def _missing_rank(f: ListFilters) -> int:
    """Highest cached rank still counted as missing: songs on another album count as owned unless opted in."""
    return saved_match.STATE_RANK["other_album"] if f.other_album else saved_match.STATE_RANK["error"]


def _order(f: ListFilters, g) -> list:
    if f.sort == "added_asc":
        return [g.c.added.asc(), g.c.pos.desc(), g.c.g.desc()]
    if f.sort == "artist":
        return [g.c.artist.asc(), g.c.title.asc(), g.c.g.asc()]
    if f.sort == "title":
        return [g.c.title.asc(), g.c.artist.asc(), g.c.g.asc()]
    return [g.c.added.desc(), g.c.pos.asc(), g.c.g.asc()]


async def _counts(session: AsyncSession, f: ListFilters, g) -> dict:
    ranks = saved_match.STATE_RANK
    row = (await session.execute(select(
        func.count(), func.sum(case((g.c.rank == ranks["library"], 1), else_=0)),
        func.sum(case((g.c.rank == ranks["queued"], 1), else_=0)),
        func.sum(case((g.c.rank == ranks["other_album"], 1), else_=0)),
        func.sum(case((g.c.rank <= _missing_rank(f), 1), else_=0)), func.max(g.c.matched_at)).select_from(g))).one()
    return {"total": row[0] or 0, "in_library": row[1] or 0, "queued": row[2] or 0, "other_album": row[3] or 0,
            "missing": row[4] or 0, "counts_as_of": _iso(row[5])}


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value else None


def _local(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC).astimezone() if value else None


def added_label(value: datetime | None) -> str | None:
    moment = _local(value)
    return f"{moment.day} {moment:%b %Y}" if moment else None


def qobuz_hires(row) -> str | None:
    bits, khz = row.qobuz_max_bits, row.qobuz_max_khz
    if not bits or not khz or (bits <= 16 and khz <= 44.1):
        return None
    return f"{bits}/{khz:g}"


def _library(match: saved_match.Match | None) -> dict | None:
    if match is None:
        return None
    track = match.track
    return {**track.quality_readout(), "state": match.state, "status": track.status, "synced": track.synced_at is not None,
            "track_id": track.id, "album": track.album,
            "local_album_key": local_albums.key_of(track.album, track.album_artist, track.artist)}


def _described(row) -> dict:
    return {"title": row.title, "artist": row.artist, "album": row.album, "album_artist": row.album_artist,
            "image_url": row.image_url, "duration_ms": row.duration_ms, "track_number": row.track_number,
            "disc_number": row.disc_number, "year": row.year, "readable": row.readable,
            "qobuz_hires": qobuz_hires(row), "qobuz_streamable": row.qobuz_streamable}


def item_context(members: list[SavedItem], match: saved_match.Match | None, *, position: int | None = None,
                 setsize: int | None = None, local_album_key: str | None = None) -> dict:
    """The per-item dict the Saved templates and the JSON variant read; `members` are the same song or album
    on several services, the first one shown."""
    rep = members[0]
    added = max((m.added_at or m.first_seen_at for m in members), default=None)
    library = _library(match)
    return {
        "id": rep.id, "source": rep.source, "kind": rep.kind, **_described(rep),
        "added_at": _iso(added), "added_label": added_label(added),
        "sources": [m.source for m in members], "members": [{"id": m.id, "source": m.source} for m in members],
        "removed": all(m.removed_at is not None for m in members),
        "library": library, "in_batch": False,
        "count_in_library": rep.entries_matched, "track_count": rep.track_count,
        "owner_is_me": rep.owner_is_me,
        "local_album_key": local_album_key or (library or {}).get("local_album_key"),
        "position": position, "setsize": setsize,
    }


def _by_group(f: ListFilters, rows) -> dict:
    members: dict = {}
    for row in rows:
        members.setdefault(row.group_key if _merged(f) else row.id, []).append(row)
    for group_rows in members.values():
        group_rows.sort(key=lambda r: (SOURCE_ORDER.get(r.source, 9), -(r.added_at or r.first_seen_at).timestamp()))
    return members


async def _page_members(session: AsyncSession, f: ListFilters, groups: list) -> dict:
    group = SavedItem.group_key if _merged(f) else SavedItem.id
    return _by_group(f, (await session.scalars(select(SavedItem).where(*_conditions(f), group.in_(groups)))).all())


async def all_groups(session: AsyncSession, f: ListFilters) -> list[list[SavedItem]]:
    """Every group the filters match, in list order (no paging): what a "Download all" covers."""
    g = _grouped(f)
    order = [row.g for row in await session.execute(select(g.c.g).order_by(*_order(f, g)))]
    members = _by_group(f, (await session.scalars(select(SavedItem).where(*_conditions(f)))).all())
    return [members[key] for key in order if key in members]


async def _album_keys(session: AsyncSession, items: list[SavedItem]) -> dict[int, str]:
    keys = {}
    for item in items:
        if item.kind == "album" and (found := await local_albums.find_key(session, item.title, item.artist or "")):
            keys[item.id] = found[0]
    return keys


async def in_batch_keys(session: AsyncSession, group_keys: set[str]) -> set[str]:
    """Which of these song keys wait in an open Download all."""
    if not group_keys:
        return set()
    rows = await session.scalars(select(SavedBatchItem.group_key).join(SavedBatch).where(
        SavedBatch.state.in_(OPEN_BATCH_STATES), SavedBatchItem.state.in_(("waiting", "fed")),
        SavedBatchItem.group_key.in_(group_keys)))
    return set(rows)


async def annotate(session: AsyncSession, groups: list[list[SavedItem]], *, first_position: int = 1,
                   setsize: int | None = None) -> list[dict]:
    """Item dicts for `groups`, songs matched against the library now rather than from the cache."""
    songs = [(m.source, m) for members in groups for m in members if m.kind == "track"]
    matches = dict(zip((id(row) for _, row in songs), await saved_match.match_rows(session, songs), strict=True))
    album_keys = await _album_keys(session, [members[0] for members in groups])
    batched = await in_batch_keys(session, {row.group_key for _, row in songs})
    contexts = [item_context(members, saved_match.best([matches.get(id(m)) for m in members]),
                             position=first_position + i, setsize=setsize, local_album_key=album_keys.get(members[0].id))
                for i, members in enumerate(groups)]
    for members, context in zip(groups, contexts, strict=True):
        context["in_batch"] = members[0].kind == "track" and members[0].group_key in batched
    return contexts


async def list_page(session: AsyncSession, f: ListFilters) -> dict:
    """One page of the Saved list plus header counts. Counts and the "Not downloaded yet" filter use the cached
    match; the items on the page are matched live."""
    g = _grouped(f)
    counts = await _counts(session, f, g)
    query = select(g.c.g)
    if f.missing:
        query = query.where(g.c.rank <= _missing_rank(f))
    shown = counts["missing"] if f.missing else counts["total"]
    pages = max(1, math.ceil(shown / f.per))
    page = min(f.page, pages)
    groups = [row.g for row in await session.execute(query.order_by(*_order(f, g)).limit(f.per)
                                                     .offset((page - 1) * f.per))]
    members = await _page_members(session, f, groups)
    ordered = [members[key] for key in groups if key in members]
    items = await annotate(session, ordered, first_position=(page - 1) * f.per + 1, setsize=shown)
    first = (page - 1) * f.per + 1 if items else 0
    return {**counts, "items": items, "shown": shown, "page": page, "pages": pages, "per": f.per,
            "first": first, "last": first + len(items) - 1 if items else 0,
            "filters": {**f.params(), "page": page}}


def _entry_context(entry: SavedEntry, match: saved_match.Match | None, in_batch: bool) -> dict:
    return {"id": entry.id, "position": entry.position, **_described(entry),
            "library": _library(match), "in_batch": in_batch}


async def entries_context(session: AsyncSession, item: SavedItem) -> list[dict]:
    entries = (await session.scalars(select(SavedEntry).where(SavedEntry.collection_id == item.id)
                                     .order_by(SavedEntry.position))).all()
    matches = await saved_match.match_rows(session, [(item.source, e) for e in entries])
    batched = await in_batch_keys(session, {e.group_key for e in entries})
    return [_entry_context(entry, match, entry.group_key in batched) for entry, match in zip(entries, matches, strict=True)]


async def artist_items(session: AsyncSession, artist: SavedItem) -> list[dict]:
    """The artist's saved songs and albums on every service, newest first."""
    name = primary(artist.title)
    if not name:
        return []
    rows = (await session.scalars(select(SavedItem).where(
        SavedItem.kind.in_(("track", "album")), SavedItem.removed_at.is_(None),
        *[SavedItem.search_text.contains(term, autoescape=True) for term in _terms(artist.title)])
        .order_by(func.coalesce(SavedItem.added_at, SavedItem.first_seen_at).desc())
        .limit(ARTIST_DETAIL_MAX))).all()
    mine = [row for row in rows if name in {primary(row.artist), primary(row.album_artist)}]
    return await annotate(session, [[row] for row in mine])
