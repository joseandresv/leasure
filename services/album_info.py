"""Album info for local albums: looked up once on MusicBrainz, then Deezer, gated hard against the
library's own songs and stored in `album_info`. A read-only overlay: nothing here writes Track rows,
tags, sidecars or file names. No DB session is open while a network call runs."""

import asyncio
import json
import logging
import math
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

import db
from config import settings
from models import AlbumInfo, utc_now
from services import artwork, deezer, local_albums
from services import musicbrainz as mb
from services.album_merge import (
    _has_words,
    artist_ok,
    edition_fit,
    ef,
    match_stats,
    merge,
    primary,
    strip_suffixes,
)
from services.deezer import DeezerUnavailable
from services.local_albums import KEY_VERSION, LOOSE_KEY, LocalAlbum
from services.musicbrainz import MusicBrainzUnavailable
from services.qobuz_client import normalise_isrc

logger = logging.getLogger(__name__)

ACCEPT = 0.80
BULK_DOWNLOAD = 0.90
PAYLOAD_VERSION = 1
NOT_FOUND_RETRY = timedelta(days=7)
ERROR_RETRY = timedelta(hours=1)
MB_MIN_SCORE = 80
MB_SEARCH_LIMIT = 25
MB_MAX_LOOKUPS = 3
MAX_ISRCS = 3
DEEZER_MAX_LOOKUPS = 2
QUEUE_MAX = 50
STATUS_CAP_S = 90.0
LOCK_STALE_S = 180.0
WEIGHTS = {"overlap": 0.35, "count_fit": 0.25, "position_fit": 0.20, "title_fit": 0.10, "search": 0.10}
NON_AUDIO_FORMATS = frozenset({"DVD-Video", "Blu-ray", "DVD", "VHS"})
_UNRANKED_FORMATS = re.compile(r'(?:\d+" )?vinyl|cassette|dvd.*|vhs|blu-ray', re.IGNORECASE)
_GATED_TYPES = ("live", "remix", "dj-mix", "demo", "interview", "spokenword")
_SCRIPT_ALIASES = {"Hang": {"Hang", "Kore"}, "Kana": {"Kana", "Jpan", "Hrkt", "Hani"}, "Latn": {"Latn"}}
_MB_RELEASE_URL = re.compile(r"https?://(?:beta\.)?musicbrainz\.org/release/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                             r"-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#].*)?", re.IGNORECASE)
_DEEZER_ALBUM_URL = re.compile(r"https?://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?album/(\d{1,20})(?:[/?#].*)?",
                               re.IGNORECASE)
SOURCE_LABELS = {"musicbrainz": "MusicBrainz", "deezer": "Deezer"}
SOURCES = ("musicbrainz", "deezer")

_queue: asyncio.Queue = asyncio.Queue()
_pending: dict[str, float] = {}
_followups: dict[str, tuple[str, str] | None] = {}
_task: asyncio.Task | None = None
_stopping = False
_open_sessions = 0
_notices: dict[str, str] = {}


@asynccontextmanager
async def _session():
    global _open_sessions
    _open_sessions += 1
    try:
        async with db.async_session() as session:
            yield session
    finally:
        _open_sessions -= 1


def _monotonic() -> float:
    return time.monotonic()


def lock_path():
    return settings.data_dir / "album_info.lock"


# --- local snapshot ---


@dataclass(frozen=True)
class LocalSong:
    id: int
    title: str
    artist: str | None
    album_artist: str | None
    isrc: str | None
    qobuz_isrc: str | None
    track_number: int | None
    disc_number: int | None
    duration_ms: int | None
    status: str
    file_path: str | None


@dataclass(frozen=True)
class LocalSnapshot:
    key: str
    name: str
    artist: str
    total_tracks: int | None
    total_tracks_values: frozenset[int]
    songs: tuple[LocalSong, ...]
    artists: frozenset[str]

    @property
    def done(self) -> list[LocalSong]:
        return [song for song in self.songs if song.status == "done"]

    @property
    def n_local(self) -> int:
        return len(self.done)

    @property
    def max_track_number(self) -> int:
        return max((song.track_number or 0 for song in self.done), default=0)

    @property
    def target(self) -> int:
        return max(self.max_track_number, self.n_local)

    @property
    def single_like(self) -> bool:
        titles = {ef(song.title) for song in self.done}
        return (self.n_local <= 2 and ef(self.name) in titles
                and (self.total_tracks is None or 1 <= self.total_tracks <= 4))

    @property
    def script(self) -> str:
        counts = Counter()
        for song in self.done:
            for ch in song.title or "":
                if "가" <= ch <= "힯" or "ᄀ" <= ch <= "ᇿ" or "㄰" <= ch <= "㆏":
                    counts["Hang"] += 1
                elif "぀" <= ch <= "ヿ" or "一" <= ch <= "鿿":
                    counts["Kana"] += 1
                elif ch.isalpha():
                    counts["Latn"] += 1
        return counts.most_common(1)[0][0] if counts else "Latn"


def snapshot(album: LocalAlbum, rows) -> LocalSnapshot:
    songs = tuple(LocalSong(id=r.id, title=r.title or "", artist=r.artist, album_artist=r.album_artist, isrc=r.isrc,
                            qobuz_isrc=r.qobuz_isrc, track_number=r.track_number, disc_number=r.disc_number,
                            duration_ms=r.duration_ms, status=r.status, file_path=r.file_path) for r in rows)
    artists = {primary(album.artist)} | {primary(s.artist) for s in songs} | {primary(s.album_artist) for s in songs}
    return LocalSnapshot(key=album.key, name=album.name, artist=album.artist, total_tracks=album.total_tracks,
                         total_tracks_values=album.total_tracks_values, songs=songs, artists=frozenset(artists - {""}))


# --- releases ---


@dataclass
class Release:
    source: str
    source_id: str
    title: str
    artist: str
    tracks: list[dict]
    release_group_id: str | None = None
    primary_type: str | None = None
    secondary_types: tuple[str, ...] = ()
    release_date: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    country: str | None = None
    media_format: str | None = None
    cover_url: str | None = None
    genres: tuple[str, ...] = ()

    @property
    def total_tracks(self) -> int:
        return len(self.tracks)

    @property
    def total_discs(self) -> int:
        return max((t["disc"] for t in self.tracks), default=1)

    @property
    def total_duration_ms(self) -> int | None:
        known = [t["duration_ms"] for t in self.tracks if t.get("duration_ms")]
        return sum(known) if known else None

    @property
    def year(self) -> int | None:
        head = (self.release_date or "")[:4]
        return int(head) if head.isdigit() and head != "0000" else None

    def disc_size(self, disc: int) -> int:
        on_disc = [t["position"] for t in self.tracks if t["disc"] == disc]
        return max([len(on_disc), *on_disc]) if on_disc else 0


def _credit(artist_credit) -> str:
    return ", ".join(c.get("name") or (c.get("artist") or {}).get("name") or "" for c in artist_credit or []
                     if c.get("name") or (c.get("artist") or {}).get("name"))


def _clean_date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value) or value.startswith("0000"):
        return None
    return value


def parse_mb_release(data: dict) -> Release:
    release_artist = _credit(data.get("artist-credit"))
    media = [m for m in data.get("media") or [] if (m.get("format") or "") not in NON_AUDIO_FORMATS]
    tracks = []
    for disc, medium in enumerate(media, 1):
        for index, track in enumerate(medium.get("tracks") or [], 1):
            recording = track.get("recording") or {}
            if recording.get("video"):
                continue
            tracks.append({"disc": disc, "position": int(track.get("position") or index),
                           "title": track.get("title") or recording.get("title") or "",
                           "artist": _credit(track.get("artist-credit") or recording.get("artist-credit")) or release_artist,
                           "duration_ms": track.get("length") or recording.get("length"),
                           "isrc": next(iter(recording.get("isrcs") or []), None),
                           "source_track_id": recording.get("id")})
    labels = data.get("label-info") or []
    group = data.get("release-group") or {}
    if (data.get("cover-art-archive") or {}).get("front"):
        cover = f"https://coverartarchive.org/release/{data['id']}/front-500"
    elif group.get("id"):
        cover = f"https://coverartarchive.org/release-group/{group['id']}/front-500"
    else:
        cover = None
    genres = sorted(data.get("genres") or group.get("genres") or [], key=lambda g: (-(g.get("count") or 0), g.get("name")))
    return Release(
        source="musicbrainz", source_id=data["id"], title=data.get("title") or "", artist=release_artist, tracks=tracks,
        release_group_id=group.get("id"), primary_type=group.get("primary-type"),
        secondary_types=tuple(group.get("secondary-types") or ()), release_date=_clean_date(data.get("date")),
        label=next(((li.get("label") or {}).get("name") for li in labels if (li.get("label") or {}).get("name")), None),
        catalog_number=next((li.get("catalog-number") for li in labels if li.get("catalog-number")), None),
        barcode=data.get("barcode") or None, country=data.get("country"),
        media_format=media[0].get("format") if media else None, cover_url=cover,
        genres=tuple(g["name"] for g in genres[:5] if g.get("name")))


_DEEZER_TYPES = {"album": ("Album", ()), "single": ("Single", ()), "ep": ("EP", ()), "compile": ("Album", ("Compilation",))}


def parse_deezer_album(data: dict, tracks: list[dict]) -> Release:
    positioned = all(t.get("track_position") and t.get("disk_number") for t in tracks)
    items = [{"disc": t["disk_number"] if positioned else 1,
              "position": t["track_position"] if positioned else index,
              "title": t.get("title") or "", "artist": (t.get("artist") or {}).get("name") or "",
              "duration_ms": int(t["duration"]) * 1000 if t.get("duration") else None,
              "isrc": t.get("isrc") or None, "source_track_id": str(t.get("id") or "")}
             for index, t in enumerate(tracks, 1)]
    primary_type, secondary = _DEEZER_TYPES.get(data.get("record_type") or "", (None, ()))
    return Release(
        source="deezer", source_id=str(data["id"]), title=data.get("title") or "",
        artist=(data.get("artist") or {}).get("name") or "", tracks=items, primary_type=primary_type,
        secondary_types=secondary, release_date=_clean_date(data.get("release_date")), label=data.get("label") or None,
        barcode=data.get("upc") or None, media_format="Digital", cover_url=data.get("cover_xl") or None,
        genres=tuple(g.get("name") for g in (data.get("genres") or {}).get("data") or [] if g.get("name")))


# --- gates and score ---


@dataclass(frozen=True)
class Score:
    confidence: float
    overlap: float
    position_fit: float | None
    count_fit: float
    title_fit: float

    @property
    def accepted(self) -> bool:
        return self.confidence >= ACCEPT


def types_ok(primary_type: str | None, secondary_types, fit: float, local: LocalSnapshot) -> bool:
    name = ef(local.name)
    for kind in (t.lower() for t in secondary_types or ()):
        if kind in _GATED_TYPES and not _has_words(name, kind.replace("-", " ")):
            return False
        if kind in ("compilation", "soundtrack") and fit != 1.0:
            return False
    return not local.single_like or (primary_type or "").lower() in ("single", "ep")


def count_ok(total: int | None, local: LocalSnapshot, matched: int = 0) -> bool:
    if total is None:
        return True
    if local.total_tracks_values:
        ok = total in local.total_tracks_values
    else:
        ok = local.max_track_number <= total <= max(math.ceil(local.target * 1.5), local.target + 4)
    return ok and total >= matched


def overlap_ok(overlap: float, position_fit: float | None, n_local: int) -> bool:
    if n_local <= 2:
        return overlap == 1.0 and position_fit in (None, 1.0)
    return overlap >= 0.75


def _positions_exist(local: LocalSnapshot, release: Release) -> bool:
    return all(song.track_number <= release.disc_size(song.disc_number or 1) for song in local.done if song.track_number)


def score(local: LocalSnapshot, release: Release, search_score: float) -> Score | None:
    """None when a hard gate fails; otherwise the weighted confidence (accepted at ACCEPT)."""
    fit = edition_fit(release.title, local.name)
    if not artist_ok(release.artist, local.artists) or fit is None:
        return None
    if not types_ok(release.primary_type, release.secondary_types, fit, local):
        return None
    if local.single_like and (not release.tracks or ef(release.tracks[0]["title"]) != ef(local.name)):
        return None
    stats = match_stats(merge(local.done, release.tracks), local.n_local)
    if not count_ok(release.total_tracks, local, stats.matched) or not _positions_exist(local, release):
        return None
    if not overlap_ok(stats.overlap, stats.position_fit, local.n_local):
        return None
    total = release.total_tracks
    count_fit = 1.0 if total in local.total_tracks_values else max(0.0, 1 - abs(total - local.target) / total)
    parts = {"overlap": stats.overlap, "count_fit": count_fit, "position_fit": stats.position_fit,
             "title_fit": fit, "search": min(search_score, 100) / 100}
    known = {name: value for name, value in parts.items() if value is not None}
    confidence = sum(WEIGHTS[name] * value for name, value in known.items()) / sum(WEIGHTS[name] for name in known)
    return Score(confidence=round(confidence, 4), overlap=stats.overlap, position_fit=stats.position_fit,
                 count_fit=count_fit, title_fit=fit)


@dataclass
class Pick:
    release: Release
    score: Score
    order: int


def best_pick(picks: list[Pick]) -> Pick | None:
    accepted = [p for p in picks if p.score.accepted]
    if not accepted:
        return None
    return max(accepted, key=lambda p: (p.score.confidence, p.score.position_fit or 0, p.score.count_fit, -p.order))


# --- MusicBrainz ---


def _mb_formats(hit: dict) -> list[str]:
    return [m.get("format") or "" for m in hit.get("media") or []]


def _mb_hit_ok(hit: dict, local: LocalSnapshot, rejected: set[str]) -> bool:
    group = hit.get("release-group") or {}
    fit = edition_fit(hit.get("title"), local.name)
    formats = _mb_formats(hit)
    return (int(hit.get("score") or 0) >= MB_MIN_SCORE and fit is not None
            and artist_ok(_credit(hit.get("artist-credit")), local.artists)
            and f"musicbrainz:{hit.get('id')}" not in rejected and f"musicbrainz-rg:{group.get('id')}" not in rejected
            and types_ok(group.get("primary-type"), group.get("secondary-types"), fit, local)
            and count_ok(hit.get("track-count"), local)
            and (not formats or any(not _UNRANKED_FORMATS.fullmatch(f) for f in formats)))


def _format_rank(formats: list[str]) -> int:
    return 0 if "Digital Media" in formats else 1 if "CD" in formats else 2


def _release_pref(hit: dict, local: LocalSnapshot, common_country: str | None) -> tuple:
    country = hit.get("country")
    script = (hit.get("text-representation") or {}).get("script")
    return (hit.get("track-count") != local.total_tracks if local.total_tracks else False,
            _format_rank(_mb_formats(hit)), hit.get("status") != "Official",
            script not in _SCRIPT_ALIASES.get(local.script, {local.script}),
            0 if country == "XW" else 1 if country and country == common_country else 2,
            hit.get("date") or "9999", hit.get("id") or "")


def rank_mb_hits(hits: list[dict], local: LocalSnapshot) -> list[list[dict]]:
    """Passing hits grouped by release group, best group first, each group's preferred release first."""
    groups: dict[str, list[dict]] = {}
    for hit in hits:
        groups.setdefault((hit.get("release-group") or {}).get("id") or hit.get("id"), []).append(hit)

    def group_rank(members: list[dict]) -> tuple:
        return (-max(edition_fit(h.get("title"), local.name) or 0 for h in members),
                min(abs((h.get("track-count") or 0) - local.target) for h in members),
                -max(int(h.get("score") or 0) for h in members))

    ranked = []
    for members in sorted(groups.values(), key=group_rank):
        common = Counter(h.get("country") for h in members if h.get("country")).most_common(1)
        ranked.append(sorted(members, key=lambda h: _release_pref(h, local, common[0][0] if common else None)))
    return ranked


def _search_artist(local: LocalSnapshot) -> str:
    """The credit's first artist: a joined "Metro Boomin, Future" never matches MB's "Future & Metro Boomin"."""
    return local.artist.split(",")[0].strip() or local.artist


async def _mb_search(local: LocalSnapshot, rejected: set[str]) -> list[dict]:
    names = [local.name]
    if (stripped := strip_suffixes(local.name)) and stripped != local.name:
        names.append(stripped)
    for name in names:
        query = f'release:"{mb.lucene(name)}" AND artist:"{mb.lucene(_search_artist(local))}"'
        if local.single_like:
            query += " AND (primarytype:single OR primarytype:ep)"
        data = await mb.get_checked("/release/", {"query": query, "limit": MB_SEARCH_LIMIT})
        passing = [hit for hit in (data or {}).get("releases") or [] if _mb_hit_ok(hit, local, rejected)]
        if passing:
            return passing
    return []


async def _mb_isrc_candidates(local: LocalSnapshot, rejected: set[str]) -> list[str]:
    isrcs = list(dict.fromkeys(i for s in local.done if (i := normalise_isrc(s.isrc) or normalise_isrc(s.qobuz_isrc))))
    counts: Counter = Counter()
    stubs: dict[str, dict] = {}
    for isrc in isrcs[:MAX_ISRCS]:
        data = await mb.get_checked(f"/isrc/{isrc}", {"inc": "releases"})
        found = {}
        for recording in (data or {}).get("recordings") or []:
            for release in recording.get("releases") or []:
                if release.get("id"):
                    found[release["id"]] = release
        stubs.update(found)
        counts.update(found.keys())
    usable = [rid for rid in counts if edition_fit(stubs[rid].get("title"), local.name) is not None
              and f"musicbrainz:{rid}" not in rejected]
    return sorted(usable, key=lambda rid: (-counts[rid], *_release_pref(stubs[rid], local, None)))[:2]


async def _mb_evaluate(mbid: str, local: LocalSnapshot, search_score: float, order: int,
                       rejected: set[str]) -> Pick | None:
    data = await mb.get_checked(f"/release/{mbid}", {"inc": mb.RELEASE_INC})
    if not data:
        return None
    release = parse_mb_release(data)
    if f"musicbrainz-rg:{release.release_group_id}" in rejected:
        return None
    result = score(local, release, search_score)
    return Pick(release, result, order) if result else None


async def best_musicbrainz(local: LocalSnapshot, rejected: set[str]) -> Pick | None:
    picks: list[Pick] = []
    lookups = 0
    for mbid in await _mb_isrc_candidates(local, rejected):
        lookups += 1
        if pick := await _mb_evaluate(mbid, local, 100, lookups, rejected):
            picks.append(pick)
    if best := best_pick(picks):
        return best
    groups = rank_mb_hits(await _mb_search(local, rejected), local)
    queue = [group[0] for group in groups[:2]]
    position = 0
    while position < len(queue) and lookups < MB_MAX_LOOKUPS:
        hit = queue[position]
        lookups += 1
        pick = await _mb_evaluate(hit["id"], local, int(hit.get("score") or 0), lookups, rejected)
        if pick:
            picks.append(pick)
        if position == 0 and (pick is None or not pick.score.accepted) and len(groups[0]) > 1:
            queue.insert(1, groups[0][1])
        position += 1
    return best_pick(picks)


# --- Deezer ---


def _deezer_hits(hits: list[dict], local: LocalSnapshot, rejected: set[str]) -> list[dict]:
    seen, passing = set(), []
    for hit in hits:
        fit = edition_fit(hit.get("title"), local.name)
        primary_type, secondary = _DEEZER_TYPES.get(hit.get("record_type") or "", (None, ()))
        if (fit is None or not artist_ok((hit.get("artist") or {}).get("name"), local.artists)
                or f"deezer:{hit.get('id')}" in rejected or not count_ok(hit.get("nb_tracks"), local)
                or not types_ok(primary_type, secondary, fit, local)):
            continue
        twin = (ef(hit.get("title")), hit.get("nb_tracks"))
        if twin not in seen:
            seen.add(twin)
            passing.append(hit)
    return sorted(passing, key=lambda h: (-(edition_fit(h.get("title"), local.name) or 0),
                                          abs((h.get("nb_tracks") or 0) - local.target)))


async def _deezer_release(album_id: str) -> Release | None:
    data = await deezer.album(album_id)
    if not data or not data.get("id"):
        return None
    return parse_deezer_album(data, await deezer.album_tracks(album_id, data.get("nb_tracks")))


async def best_deezer(local: LocalSnapshot, rejected: set[str]) -> Pick | None:
    artist = _search_artist(local)
    passing = _deezer_hits(await deezer.search_albums(artist, local.name), local, rejected)
    if not passing and (stripped := strip_suffixes(local.name)) and stripped != local.name:
        passing = _deezer_hits(await deezer.search_albums(artist, stripped), local, rejected)
    picks = []
    for order, hit in enumerate(passing[:DEEZER_MAX_LOOKUPS]):
        release = await _deezer_release(str(hit["id"]))
        if release and (result := score(local, release, 100)):
            picks.append(Pick(release, result, order))
    return best_pick(picks)


# --- pinned releases ---


def parse_release_url(url: str) -> tuple[str, str] | None:
    url = (url or "").strip()
    if match := _MB_RELEASE_URL.fullmatch(url):
        return "musicbrainz", match[1].lower()
    if match := _DEEZER_ALBUM_URL.fullmatch(url):
        return "deezer", match[1]
    return None


async def _pinned(local: LocalSnapshot, pin: tuple[str, str]) -> Pick | None:
    source, source_id = pin
    if source == "musicbrainz":
        data = await mb.get_checked(f"/release/{source_id}", {"inc": mb.RELEASE_INC})
        release = parse_mb_release(data) if data else None
    else:
        release = await _deezer_release(source_id)
    if release is None:
        return None
    stats = match_stats(merge(local.done, release.tracks), local.n_local)
    return Pick(release, Score(confidence=1.0, overlap=stats.overlap, position_fit=stats.position_fit,
                               count_fit=1.0, title_fit=edition_fit(release.title, local.name) or 0.0), 0)


# --- persistence ---


@dataclass(frozen=True)
class InfoSnapshot:
    status: str
    source: str | None
    source_id: str | None
    release_group_id: str | None
    pinned: bool
    cover_url: str | None
    rejected: tuple[str, ...]
    rejected_json: str | None


def rejected_ids(row: AlbumInfo | None) -> list[str]:
    try:
        value = json.loads(row.rejected_json) if row and row.rejected_json else []
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _info_snapshot(row: AlbumInfo | None) -> InfoSnapshot | None:
    if row is None:
        return None
    return InfoSnapshot(status=row.status, source=row.source, source_id=row.source_id,
                        release_group_id=row.release_group_id, pinned=bool(row.pinned), cover_url=row.cover_url,
                        rejected=tuple(rejected_ids(row)), rejected_json=row.rejected_json)


_FOUND_FIELDS = ("source", "source_id", "release_group_id", "confidence", "match_overlap", "match_position_fit", "title",
                 "artist", "release_date", "year", "label", "catalog_number", "barcode", "country", "media_format",
                 "total_tracks", "total_discs", "total_duration_ms", "cover_url", "genres_json", "tracks_json")


def _unchanged_since(existing: InfoSnapshot | None):
    """A "Not this album" committed while the lookup ran changes rejected_json; the lookup must not undo it."""
    return None if existing is None else AlbumInfo.rejected_json.is_not_distinct_from(existing.rejected_json)


async def _upsert(local: LocalSnapshot, values: dict, existing: InfoSnapshot | None = None) -> bool:
    row = {"key_version": KEY_VERSION, "key_album_raw": local.name, "key_artist_raw": local.artist, **values}
    stmt = sqlite_insert(AlbumInfo).values(album_key=local.key, **row)
    stmt = stmt.on_conflict_do_update(index_elements=["album_key"], set_=row, where=_unchanged_since(existing))
    async with _session() as session:
        result = await session.execute(stmt)
        await session.commit()
    return result.rowcount != 0


async def _update(key: str, values: dict, existing: InfoSnapshot) -> bool:
    stmt = update(AlbumInfo).where(AlbumInfo.album_key == key, _unchanged_since(existing)).values(**values)
    async with _session() as session:
        result = await session.execute(stmt)
        await session.commit()
    return result.rowcount != 0


def _found_values(pick: Pick, pinned: bool, rejected: tuple[str, ...]) -> dict:
    release, result = pick.release, pick.score
    return {
        "status": "found", "source": release.source, "source_id": release.source_id,
        "release_group_id": release.release_group_id, "pinned": pinned, "confidence": result.confidence,
        "match_overlap": result.overlap, "match_position_fit": result.position_fit,
        "rejected_json": json.dumps(list(rejected)), "title": release.title, "artist": release.artist,
        "release_date": release.release_date, "year": release.year, "label": release.label,
        "catalog_number": release.catalog_number, "barcode": release.barcode, "country": release.country,
        "media_format": release.media_format, "total_tracks": release.total_tracks, "total_discs": release.total_discs,
        "total_duration_ms": release.total_duration_ms, "cover_url": release.cover_url,
        "genres_json": json.dumps(list(release.genres)), "tracks_json": json.dumps(release.tracks),
        "payload_version": PAYLOAD_VERSION, "attempts": 0, "last_error": None, "fetched_at": utc_now(),
        "retry_at": None,
    }


def _error_text(error: Exception) -> str:
    if isinstance(error, MusicBrainzUnavailable):
        return f"MusicBrainzUnavailable {error.status or error.detail}".strip()
    if isinstance(error, DeezerUnavailable):
        return f"DeezerUnavailable {error.detail}"
    return error.__class__.__name__


async def _record_unsettled(local: LocalSnapshot, existing: InfoSnapshot | None, attempts: int,
                            status: str, last_error: str | None, notice: str | None = None) -> str:
    """No new release: a found row keeps its data (and is re-checked later); otherwise not_found / error.
    "superseded" when the user rejected the release meanwhile."""
    retry = utc_now() + (ERROR_RETRY if status == "error" else NOT_FOUND_RETRY)
    keep = existing is not None and existing.status == "found"
    if keep and not notice:
        label = SOURCE_LABELS.get(existing.source or "", "the current source")
        notice = ("Album info service unavailable; kept the current info." if status == "error"
                  else f"No other match found; keeping the info from {label}.")
    if keep:
        written = await _update(local.key, {"attempts": attempts + 1, "last_error": last_error, "retry_at": retry},
                                existing)
    else:
        written = await _upsert(local, {"status": status, "attempts": attempts + 1, "last_error": last_error,
                                        "fetched_at": utc_now(), "retry_at": retry, "pinned": False,
                                        "payload_version": PAYLOAD_VERSION,
                                        "rejected_json": json.dumps(list(existing.rejected if existing else ())),
                                        **dict.fromkeys(_FOUND_FIELDS)}, existing)
    if not written:
        return "superseded"
    if notice:
        _notices[local.key] = notice
    return "kept" if keep else status


def _write_cover(path, jpeg: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(jpeg)
    os.replace(tmp, path)


def _has_sidecar(songs) -> bool:
    return any(artwork.sidecar_art_path(song.file_path) for song in songs if song.status == "done")


async def _fetch_cover(url: str) -> bytes | None:
    return await artwork.fetch_artwork(url)


async def _cache_cover(local: LocalSnapshot, cover_url: str | None, changed: bool) -> None:
    """Fetch the release cover once, only for an album none of whose songs has a sidecar."""
    if not cover_url or await asyncio.to_thread(_has_sidecar, local.songs):
        return
    path = artwork.album_cover_path(local.key)
    if not changed and await asyncio.to_thread(path.is_file):
        return
    data = await _fetch_cover(cover_url)
    jpeg = await asyncio.to_thread(artwork.prepare_cover, data, settings.artwork_size) if data else None
    if jpeg:
        await asyncio.to_thread(_write_cover, path, jpeg)


async def _read_state(key: str) -> tuple[LocalSnapshot | None, InfoSnapshot | None, int]:
    async with _session() as session:
        loaded = await local_albums.load(session, key)
        row = await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == key))
        attempts = (row.attempts or 0) if row else 0
        return (snapshot(*loaded) if loaded else None), _info_snapshot(row), attempts


async def _search(local: LocalSnapshot, rejected: set[str], sources: tuple[str, ...]) -> Pick | None:
    pick = await best_musicbrainz(local, rejected) if "musicbrainz" in sources else None
    return pick or (await best_deezer(local, rejected) if "deezer" in sources else None)


async def lookup(key: str, *, pin: tuple[str, str] | None = None, sources: tuple[str, ...] = SOURCES) -> str:
    """Find and store album info for one local album: found | not_found | error | kept | skipped | superseded.
    MusicBrainz first; Deezer only when MusicBrainz answered and nothing passed the gates."""
    if key == LOOSE_KEY:
        return "skipped"
    local, existing, attempts = await _read_state(key)
    if local is None:
        return "skipped"
    rejected = set(existing.rejected if existing else ())
    if pin is None and existing and existing.pinned and existing.status == "found" and existing.source_id:
        pin = (existing.source, existing.source_id)
    try:
        if pin:
            pick = await _pinned(local, pin)
        else:
            pick = await _search(local, rejected, sources)
    except (MusicBrainzUnavailable, DeezerUnavailable) as e:
        logger.info("Album info for %s unavailable: %s", key, _error_text(e))
        return await _record_unsettled(local, existing, attempts, "error", _error_text(e))
    except Exception as e:
        logger.exception("Album info lookup for %s failed", key)
        return await _record_unsettled(local, existing, attempts, "error", _error_text(e))
    if pick is None:
        notice = "That release could not be found." if pin else None
        return await _record_unsettled(local, existing, attempts, "not_found", None, notice)
    if not await _upsert(local, _found_values(pick, bool(pin), existing.rejected if existing else ()), existing):
        logger.info("Album info for %s changed during its lookup; result dropped", key)
        return "superseded"
    changed = existing is None or existing.cover_url != pick.release.cover_url
    try:
        await _cache_cover(local, pick.release.cover_url, changed)
    except OSError as e:
        logger.warning("Could not store the cover of album %s: %s", key, e.__class__.__name__)
    logger.info("Album info for %s: %s %s (confidence %.2f)", key, pick.release.source, pick.release.source_id,
                pick.score.confidence)
    return "found"


async def reject(session: AsyncSession, key: str) -> bool:
    """"Not this album": remember the release (and its group) as wrong, clear it and its cached cover, look again."""
    row = await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == key))
    if row is None or row.status != "found":
        return False
    rejected = rejected_ids(row)
    rejected.append(f"{row.source}:{row.source_id}")
    if row.source == "musicbrainz" and row.release_group_id:
        rejected.append(f"musicbrainz-rg:{row.release_group_id}")
    for name in _FOUND_FIELDS:
        setattr(row, name, None)
    row.rejected_json = json.dumps(list(dict.fromkeys(rejected)))
    row.status, row.pinned, row.retry_at = "not_found", False, utc_now()
    await session.commit()
    await asyncio.to_thread(artwork.album_cover_path(key).unlink, missing_ok=True)
    if key in _pending:
        _followups.setdefault(key, None)
    return True


def pop_notice(key: str) -> str | None:
    return _notices.pop(key, None)


# --- the background job ---


def _script_lock_held() -> bool:
    try:
        return time.time() - lock_path().stat().st_mtime < LOCK_STALE_S
    except OSError:
        return False


def lookups_open() -> bool:
    """A lookup queued now would run: enabled, not shutting down, no prefetch script holding the lock."""
    return settings.album_info_lookups and not _stopping and not _script_lock_held()


def release_tracks(row: AlbumInfo | None) -> list[dict] | None:
    if row is None or row.status != "found" or not row.tracks_json:
        return None
    try:
        tracks = json.loads(row.tracks_json)
    except ValueError:
        return None
    return tracks if isinstance(tracks, list) else None


def mismatched(row: AlbumInfo | None, rows) -> bool:
    """A non-pinned found release that no longer fits the group's songs (view-time re-check)."""
    tracks = release_tracks(row)
    if tracks is None or row.pinned:
        return False
    done = [r for r in rows if r.status == "done"]
    stats = match_stats(merge(done, tracks), len(done))
    return not overlap_ok(stats.overlap, stats.position_fit, len(done))


async def due_state(session: AsyncSession, key: str) -> tuple[AlbumInfo | None, bool]:
    row = await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == key))
    if row is None or row.status != "found":
        return row, False
    loaded = await local_albums.load(session, key)
    return row, bool(loaded) and mismatched(row, loaded[1])


def is_due(row: AlbumInfo | None, mismatch: bool) -> bool:
    now = utc_now()
    if row is None or (row.payload_version or 0) < PAYLOAD_VERSION:
        return True
    if row.status == "found":
        return mismatch and (row.retry_at is None or row.retry_at <= now)
    return row.retry_at is None or row.retry_at <= now


async def ensure(session: AsyncSession, key: str, *, force: bool = False, pin: tuple[str, str] | None = None) -> str:
    """Queue a lookup when one is due: pending | settled | disabled | busy | never. Never waits for the network."""
    if key == LOOSE_KEY:
        return "never"
    if not settings.album_info_lookups or _stopping:
        return "disabled"
    if key in _pending:
        if pin is not None:
            _followups[key] = pin
        return "pending"
    row, mismatch = await due_state(session, key)
    if not force and not is_due(row, mismatch):
        return "settled"
    if _script_lock_held() or _queue.qsize() >= QUEUE_MAX:
        return "busy"
    _pending[key] = _monotonic()
    _queue.put_nowait((key, pin))
    _start_worker()
    return "pending"


def pending_for(key: str) -> float | None:
    """Seconds since `key` was queued, or None when no lookup is pending for it."""
    started = _pending.get(key)
    return None if started is None else _monotonic() - started


def _start_worker() -> None:
    global _task
    if not _stopping and (_task is None or _task.done()):
        _task = asyncio.create_task(_worker())


async def _worker() -> None:
    while True:
        key, pin = await _queue.get()
        try:
            if _script_lock_held():
                # The enrich script throttles in its own process; both together would exceed MB's 1 req/s.
                logger.info("Album info lookup for %s skipped: the enrich script is running", key)
                _followups.pop(key, None)
            else:
                await lookup(key, pin=pin)
        except Exception:
            logger.exception("Album info lookup failed for %s", key)
        finally:
            _requeue_or_settle(key)
            _queue.task_done()


def _requeue_or_settle(key: str) -> None:
    """A pin or "Not this album" that arrived while `key` was being looked up runs next; else the key settles."""
    if key in _followups and not _stopping:
        _pending[key] = _monotonic()
        _queue.put_nowait((key, _followups.pop(key)))
    else:
        _followups.pop(key, None)
        _pending.pop(key, None)


async def stop() -> None:
    global _stopping, _task, _queue
    _stopping = True
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    _pending.clear()
    _followups.clear()
    _queue = asyncio.Queue()


def reset_for_tests() -> None:
    global _stopping, _task, _queue
    if _task is not None and not _task.done():
        _task.cancel()
    _task, _stopping, _queue = None, False, asyncio.Queue()
    _pending.clear()
    _followups.clear()
    _notices.clear()


# --- maintenance ---


async def rekey_stale_rows(session: AsyncSession) -> int:
    """Re-key rows written under an older KEY_VERSION from their stored raw names; no network."""
    stale = (await session.scalars(select(AlbumInfo).where(AlbumInfo.key_version != KEY_VERSION))).all()
    for row in stale:
        new = local_albums.album_key(row.key_album_raw, row.key_artist_raw, row.key_artist_raw)
        holder = await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == new.key)) if new else None
        if new is None or (holder is not None and holder.fetched_at >= row.fetched_at):
            await session.delete(row)
            continue
        if holder is not None:
            await session.delete(holder)
            await session.flush()
        row.album_key, row.key_version = new.key, KEY_VERSION
    if stale:
        await session.commit()
    return len(stale)


async def info_sources(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(select(AlbumInfo.album_key, AlbumInfo.source).where(AlbumInfo.status == "found"))
    return {key: source for key, source in rows}


async def prune(session: AsyncSession, live_keys: set[str]) -> list[str]:
    """Delete album_info rows (and cached covers) whose album is no longer in the library."""
    keys = [k for k in (await session.scalars(select(AlbumInfo.album_key))).all() if k not in live_keys]
    if keys:
        await session.execute(delete(AlbumInfo).where(AlbumInfo.album_key.in_(keys)))
        await session.commit()
    for key in keys:
        await asyncio.to_thread(artwork.album_cover_path(key).unlink, missing_ok=True)
    return keys
