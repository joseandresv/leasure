"""Downloads from the Saved snapshot: what a saved song becomes in the library, the check that never queues a
song the library has or is getting, and the numbers a "Download all" confirms before it becomes a batch."""

import asyncio
import hashlib
import hmac
import json
import math
import secrets
import shutil
from collections import Counter
from dataclasses import asdict, dataclass, field, replace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import Playlist, PlaylistTrack, Track, utc_now
from models_saved import SavedBatch, SavedBatchItem, SavedEntry, SavedItem
from services import library_match, qobuz_engine, qobuz_session, saved_listing, yt_engine
from services.album_merge import ef
from services.download_lock import DOWNLOAD_LOCK
from services.downloader import BULK_YT_RESERVE, QOBUZ_ROUTED
from services.formats import resolve_format
from services.library_match import LibraryIndex, TrackKeys
from services.saved_listing import OPEN_BATCH_STATES, SOURCE_ORDER, ListFilters
from services.tracks import upsert_track

DETAIL_NO_CONFIRM_MAX = 50
BATCH_MAX = 5000
SPACE_MARGIN_GB = 20
DEFAULT_SONG_S = 240
FLAC_RATIO = 0.7
MP3_BYTES_PER_S = 40_000
# The Download all lane: at most this many songs started an hour, a random gap after each one finishes.
BULK_MAX_PER_HOUR = 20
BULK_GAP_S = (120.0, 300.0)
_GB = 1000**3
_ID_FIELDS = ("qobuz_id", "spotify_uri", "youtube_id")
# A batch item's state for each reason a song is not queued.
SKIP_STATES = {"library": "skipped_library", "other_album": "skipped_other_album", "queued": "skipped_queued",
               "duplicate": "skipped_duplicate", "unreadable": "skipped_unreadable", "not_streamable": "not_streamable"}
_MAX_BITS_KHZ = {6: (16, 44.1), 7: (24, 96.0), 27: (24, 192.0)}
_SOURCE_LABELS = {"spotify": "Spotify", "qobuz": "Qobuz", "youtube": "YT Music", "all": "All sources"}
_KIND_LABELS = {"track": "Songs", "album": "Albums", "playlist": "Playlists", "artist": "Artists"}
_TOKEN_KEY = secrets.token_bytes(32)


@dataclass
class Pick:
    """One song to download: `source` is the service the row is downloaded as (Qobuz first, then Spotify, then
    YouTube), `data` the Track fields, carrying every id the song is known by on the services that saved it."""
    source: str
    data: dict
    group_key: str
    readable: bool = True
    streamable: bool | None = None
    bits: int | None = None
    khz: float | None = None
    saved_item_id: int | None = None
    entry_id: int | None = None
    playlist: dict | None = None            # {"source", "source_id", "name", "image_url"}
    playlist_position: int | None = None

    def keys(self) -> TrackKeys:
        d = self.data
        return TrackKeys(title=d["title"], artist=d["artist"], album=d.get("album") or "", isrc=d.get("isrc"),
                         **{name: d.get(name) for name in _ID_FIELDS})

    def markers(self) -> set[str]:
        return {f"g:{self.group_key}"} | {f"{name}:{self.data[name]}" for name in _ID_FIELDS if self.data.get(name)}

    @property
    def qobuz_only(self) -> bool:
        return self.source == "qobuz" and not (self.data.get("spotify_uri") or self.data.get("youtube_id"))

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Pick":
        return cls(**json.loads(text))


def _ids(source: str, row) -> dict:
    if source == "qobuz":
        return {"qobuz_id": row.source_id}
    if source == "youtube":
        return {"youtube_id": row.source_id}
    uri = row.uri or ""
    return {"spotify_uri": uri} if uri.startswith("spotify:track:") else {}


def make_pick(members: list[tuple[str, object]], **pointers) -> Pick:
    """A pick from the same song saved on one or more services (rows: SavedItem or SavedEntry)."""
    members = sorted(members, key=lambda m: SOURCE_ORDER.get(m[0], 9))
    playable = [(source, row) for source, row in members if row.readable]
    source, rep = (playable or members)[0]
    data = {"title": rep.title or "", "artist": rep.artist or "", "album": rep.album, "album_artist": rep.album_artist,
            "track_number": rep.track_number, "disc_number": rep.disc_number, "total_tracks": rep.total_tracks,
            "total_discs": rep.total_discs, "duration_ms": rep.duration_ms, "year": rep.year,
            "artwork_url": rep.image_url, "isrc": next((row.isrc for _, row in playable if row.isrc), None)}
    for member_source, row in playable:
        for name, value in _ids(member_source, row).items():
            data.setdefault(name, value)
    qobuz_rows = [row for member_source, row in members if member_source == "qobuz"]
    return Pick(source=source, data=data, group_key=rep.group_key, readable=bool(playable),
                streamable=qobuz_rows[0].qobuz_streamable if qobuz_rows and not playable else None,
                bits=qobuz_rows[0].qobuz_max_bits if qobuz_rows else None,
                khz=qobuz_rows[0].qobuz_max_khz if qobuz_rows else None, **pointers)


async def song_picks(session: AsyncSession, item: SavedItem) -> list[Pick]:
    """A saved song together with the same song saved on the other services."""
    members = (await session.scalars(select(SavedItem).where(
        SavedItem.kind == "track", SavedItem.group_key == item.group_key, SavedItem.removed_at.is_(None),
        SavedItem.id != item.id))).all() if item.group_key else []
    return [make_pick([(m.source, m) for m in (item, *members)], saved_item_id=item.id)]


async def collection_picks(session: AsyncSession, item: SavedItem) -> list[Pick]:
    entries = (await session.scalars(select(SavedEntry).where(SavedEntry.collection_id == item.id)
                                     .order_by(SavedEntry.position))).all()
    playlist = ({"source": item.source, "source_id": item.source_id, "name": item.title, "image_url": item.image_url}
                if item.kind == "playlist" else None)
    return [make_pick([(item.source, entry)], entry_id=entry.id, playlist=playlist, playlist_position=entry.position)
            for entry in entries]


# --- what the library already has ---


@dataclass
class Dedupe:
    index: LibraryIndex
    songs: LibraryIndex
    include_other_album: bool
    in_batches: set[str] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)


async def batch_markers(session: AsyncSession, batch_id: int | None = None) -> set[str]:
    """Songs waiting in open batches (or in one batch), as pick markers."""
    stmt = (select(SavedBatchItem.group_key, SavedBatchItem.data).join(SavedBatch)
            .where(SavedBatchItem.kind == "track", SavedBatchItem.state.in_(("waiting", "fed"))))
    stmt = stmt.where(SavedBatch.id == batch_id) if batch_id else stmt.where(SavedBatch.state.in_(OPEN_BATCH_STATES))
    markers: set[str] = set()
    for _group_key, data in await session.execute(stmt):
        markers |= Pick.from_json(data).markers()
    return markers


async def dedupe_context(session: AsyncSession, picks: list[Pick], include_other_album: bool, *,
                         batches: bool = True) -> Dedupe:
    index = await library_match.library_index(session, [p.keys() for p in picks])
    songs = await library_match.song_index(session, {ef(p.data["title"]) for p in picks if p.data["title"]})
    return Dedupe(index=index, songs=songs, include_other_album=include_other_album,
                  in_batches=await batch_markers(session) if batches else set())


def classify(pick: Pick, d: Dedupe) -> tuple[str, Track | None]:
    """("queue", an errored row to reuse or None), or why not: library | other_album | queued | duplicate |
    unreadable | not_streamable, with the library row when there is one."""
    if not pick.readable:
        return ("not_streamable" if pick.streamable is False else "unreadable"), None
    hit = d.index.find(pick.keys())
    if hit is not None and hit.status != "error":
        return ("library" if hit.status == "done" else "queued"), hit
    if hit is None:
        data = pick.data
        other = d.songs.find_song(data["title"], {data["artist"], data.get("album_artist") or ""}, data["duration_ms"])
        if other is not None and other.status != "error":
            if other.status != "done":
                return "queued", other
            if not d.include_other_album:
                return "other_album", other
    markers = pick.markers()
    if markers & d.in_batches:
        return "queued", None
    if markers & d.seen:
        return "duplicate", None
    d.seen |= markers
    return "queue", hit


def request_for(pick: Pick, fmt: str, *, bulk: bool) -> tuple[str, str]:
    """(quality, container) of a pick's row. A Download all of a song only Qobuz has is FLAC-only: it is a
    Qobuz favorite, and its fallback would be a search."""
    quality, container = resolve_format(fmt)
    if bulk and pick.qobuz_only and quality in QOBUZ_ROUTED:
        return "flac_lossless", "flac"
    return quality, container


def route_of(quality: str) -> str:
    return "qobuz" if quality in QOBUZ_ROUTED else "youtube"


async def create_row(session: AsyncSession, pick: Pick, quality: str, container: str, existing: Track | None) -> Track:
    track = await upsert_track(session, pick.data, quality=quality, container=container, source=pick.source,
                               existing=existing)
    if pick.data.get("qobuz_id"):
        track.qobuz_id = pick.data["qobuz_id"]
        if pick.source == "qobuz":
            track.qobuz_match_method = "id"
    return track


async def link_playlist(session: AsyncSession, cache: dict, pick: Pick, track_id: int, *, reset: bool) -> None:
    """Keep the saved playlist as a Playlist row so the next card sync writes its .m3u8. `reset` (a whole
    playlist queued at once) rebuilds its order; otherwise the song is added at its place."""
    ref = pick.playlist
    key = (ref["source"], ref["source_id"])
    playlist = cache.get(key)
    if playlist is None:
        playlist = await session.scalar(select(Playlist).where(Playlist.source == ref["source"],
                                                                Playlist.source_id == ref["source_id"]))
        if playlist is None:
            playlist = Playlist(name=ref["name"] or "Untitled Playlist", source=ref["source"],
                                source_id=ref["source_id"], image_url=ref.get("image_url"))
            session.add(playlist)
            await session.flush()
        else:
            playlist.name = ref["name"] or playlist.name
            if reset:
                await session.execute(PlaylistTrack.__table__.delete().where(PlaylistTrack.playlist_id == playlist.id))
        cache[key] = playlist
    present = await session.scalar(select(PlaylistTrack.id).where(PlaylistTrack.playlist_id == playlist.id,
                                                                  PlaylistTrack.track_id == track_id).limit(1))
    if present is None:
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track_id, position=pick.playlist_position or 0))


@dataclass
class QueueResult:
    queued: list[int] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    track_ids: list[int | None] = field(default_factory=list)

    def badge(self) -> dict:
        """The download_badge.html context for one song."""
        verdict = next(iter(self.counts), "queue")
        track_id = self.track_ids[0] if self.track_ids else None
        return {"queue": {"status": "queued", "track_id": track_id},
                "library": {"status": "already_downloaded", "track_id": track_id},
                "other_album": {"status": "already_downloaded", "track_id": track_id,
                                "message": "In your library on another album."},
                "queued": {"status": "already_queued", "track_id": track_id},
                "not_streamable": {"status": "error", "message": "Qobuz cannot stream this song."},
                }.get(verdict, {"status": "error", "message": "This song cannot be downloaded."})

    def summary(self) -> dict:
        return {"queued": len(self.queued), "skipped": {k: v for k, v in self.counts.items() if k != "queue"},
                "track_ids": self.queued}


async def queue_saved(session: AsyncSession, picks: list[Pick], fmt: str, enqueue, *,
                      include_other_album: bool = False) -> QueueResult:
    """Queue `picks` now (a song, or a collection's missing songs): never a song the library has or is getting.
    A song still waiting in a Download all is queued anyway; the batch skips it when its turn comes."""
    result = QueueResult()
    async with DOWNLOAD_LOCK:
        d = await dedupe_context(session, picks, include_other_album, batches=False)
        playlists: dict = {}
        for pick in picks:
            verdict, track = classify(pick, d)
            if verdict == "queue":
                track = await create_row(session, pick, *request_for(pick, fmt, bulk=False), existing=track)
                result.queued.append(track.id)
            result.counts[verdict] += 1
            result.track_ids.append(track.id if track else None)
            if pick.playlist and track is not None:
                await link_playlist(session, playlists, pick, track.id, reset=True)
        await session.commit()
    for track_id in result.queued:
        await enqueue(track_id)
    return result


# --- Download all: selection, numbers, batch ---


@dataclass
class Selection:
    """What a Download all covers, in list order: picks, and saved albums whose songs are read later."""
    scope: str
    units: list = field(default_factory=list)       # Pick | SavedItem (an album still to read)
    not_read: int = 0                               # playlists whose songs were never read, or unreadable

    @property
    def picks(self) -> list[Pick]:
        return [u for u in self.units if isinstance(u, Pick)]

    @property
    def pending(self) -> list[SavedItem]:
        return [u for u in self.units if not isinstance(u, Pick)]

    def ids(self) -> list[str]:
        def unit_id(unit) -> str:
            if not isinstance(unit, Pick):
                return f"c{unit.id}"
            return f"i{unit.saved_item_id}" if unit.saved_item_id else f"e{unit.entry_id}"

        return sorted(unit_id(u) for u in self.units)


def scope_text(f: ListFilters) -> str:
    text = f"{_SOURCE_LABELS.get(f.source, f.source)} · {_KIND_LABELS.get(f.kind, f.kind)}"
    return f"{text} · matching “{f.q}”" if f.q else text


async def _add_collection(session: AsyncSession, selection: Selection, item: SavedItem) -> None:
    if item.entries_fetched_at is not None:
        selection.units += await collection_picks(session, item)
    elif item.kind == "album" and item.readable:
        selection.units.append(item)
    else:
        selection.not_read += 1


async def selection_for_filters(session: AsyncSession, f: ListFilters) -> Selection:
    f = replace(f, removed=False)
    selection = Selection(scope=scope_text(f))
    for members in await saved_listing.all_groups(session, f):
        if f.kind == "track":
            selection.units.append(make_pick([(m.source, m) for m in members], saved_item_id=members[0].id))
        else:
            read = next((m for m in members if m.entries_fetched_at is not None), members[0])
            await _add_collection(session, selection, read)
    return selection


async def selection_for_item(session: AsyncSession, item: SavedItem) -> Selection:
    selection = Selection(scope=f"{item.title} ({_SOURCE_LABELS.get(item.source, item.source)})")
    if item.kind == "track":
        selection.units = await song_picks(session, item)
    else:
        await _add_collection(session, selection, item)
    return selection


def _estimate_bytes(pick: Pick | None, quality: str) -> float:
    seconds = (pick.data.get("duration_ms") or 0) / 1000 if pick else 0
    seconds = seconds or DEFAULT_SONG_S
    if quality not in QOBUZ_ROUTED:
        return seconds * MP3_BYTES_PER_S
    max_bits, max_khz = _MAX_BITS_KHZ[settings.qobuz_max_format]
    bits = min(pick.bits or 16, max_bits) if pick else 16
    khz = min(pick.khz or 44.1, max_khz) if pick else 44.1
    return seconds * bits * khz * 1000 * 2 / 8 * FLAC_RATIO


def _free_bytes() -> int:
    return shutil.disk_usage(settings.library_dir).free


def daily_room(route: str) -> tuple[int, int]:
    """(songs a day the bulk lane may start on `route`, how many of them are left today). Blocking."""
    if route == "qobuz":
        per_day = qobuz_engine.daily_share("bulk")
        cap_left = settings.qobuz_max_downloads_per_day - qobuz_engine.downloads_today()
        return per_day, max(0, min(qobuz_engine.share_left("bulk"), cap_left))
    per_day = max(0, yt_engine.MAX_DOWNLOADS_PER_DAY - BULK_YT_RESERVE)
    return per_day, max(0, per_day - yt_engine.downloads_today())


def days_at_least(songs: int, per_day: int, left_today: int) -> int | None:
    """Days a batch takes at the lane's daily share, counting what is left of today's; None when it never starts."""
    if songs <= 0:
        return 0
    if per_day <= 0:
        return None
    if songs <= left_today:
        return 1
    return math.ceil((songs - left_today) / per_day) + (1 if left_today else 0)


def lane_route(quality: str) -> str:
    """Best without any Qobuz account downloads everything as MP3 from YouTube."""
    stored = qobuz_session.load()
    if quality == "native" and (stored is None or stored.state == "ineligible"):
        return "youtube"
    return route_of(quality)


@dataclass
class Plan:
    selection: Selection
    fmt: str
    include_other_album: bool
    verdicts: list[tuple[Pick, str, Track | None]]
    counts: Counter
    numbers: dict
    token: str
    refused: str | None


async def snapshot_version(session: AsyncSession) -> list[str]:
    items = (await session.execute(select(func.count(SavedItem.id), func.max(SavedItem.last_seen_at),
                                          func.max(SavedItem.removed_at)))).one()
    entries = (await session.execute(select(func.count(SavedEntry.id), func.max(SavedEntry.id)))).one()
    return [str(value) for value in (*items, *entries)]


def _token(selection: Selection, fmt: str, include_other_album: bool, version: list[str]) -> str:
    payload = json.dumps({"ids": selection.ids(), "fmt": fmt, "other": include_other_album, "v": version})
    return hmac.new(_TOKEN_KEY, payload.encode(), hashlib.sha256).hexdigest()[:40]


def _refusal(numbers: dict) -> str | None:
    if numbers["total"] == 0:
        return "Nothing to download: every song here is in your library or queued already."
    if numbers["total"] > BATCH_MAX:
        return f"That is {numbers['total']:,} songs; one Download all takes at most {BATCH_MAX:,}. Narrow the filter."
    if not numbers["space_ok"]:
        return (f"Not enough free space: these songs need about {numbers['size_gb']:,.1f} GB and Leasure keeps "
                f"{SPACE_MARGIN_GB} GB free; the library drive has {numbers['free_gb']:,.0f} GB.")
    if numbers["days_at_least"] is None:
        return "The daily download limit for this format is 0, so these songs would never start."
    return None


async def make_plan(session: AsyncSession, selection: Selection, fmt: str, *, include_other_album: bool) -> Plan:
    """The confirmation's numbers: the same dedupe queue_saved runs, read-only. ValueError on an unknown format."""
    quality, _ = resolve_format(fmt)
    d = await dedupe_context(session, selection.picks, include_other_album)
    verdicts = []
    for pick in selection.picks:
        verdict, track = classify(pick, d)
        verdicts.append((pick, verdict, track))
    counts = Counter(verdict for _, verdict, _ in verdicts)
    to_queue = [pick for pick, verdict, _ in verdicts if verdict == "queue"]
    pending_tracks = sum(item.track_count or 0 for item in selection.pending)
    route = await asyncio.to_thread(lane_route, quality)
    size = sum(_estimate_bytes(p, request_for(p, fmt, bulk=True)[0]) for p in to_queue)
    size += pending_tracks * _estimate_bytes(None, "mp3_320" if route == "youtube" else quality)
    free = await asyncio.to_thread(_free_bytes)
    per_day, left_today = await asyncio.to_thread(daily_room, route)
    total = len(to_queue) + pending_tracks
    numbers = {
        "scope": selection.scope, "format": fmt, "route": route, "total": total, "songs": len(to_queue),
        "in_library": counts["library"], "other_album": counts["other_album"], "include_other_album": include_other_album,
        "queued": counts["queued"], "duplicates": counts["duplicate"], "not_streamable": counts["not_streamable"],
        "unreadable": counts["unreadable"], "albums_to_read": len(selection.pending), "album_songs_estimated": pending_tracks,
        "playlists_not_read": selection.not_read, "size_gb": round(size / _GB, 1), "free_gb": round(free / _GB, 1),
        "space_margin_gb": SPACE_MARGIN_GB, "space_ok": size + SPACE_MARGIN_GB * _GB <= free,
        "per_day": per_day, "left_today": left_today, "per_hour": BULK_MAX_PER_HOUR,
        "days_at_least": days_at_least(total, per_day, left_today),
        "qobuz_daily_cap": settings.qobuz_max_downloads_per_day,
        "onedrive": "onedrive" in str(settings.library_dir).casefold(),
    }
    token = _token(selection, fmt, include_other_album, await snapshot_version(session))
    return Plan(selection=selection, fmt=fmt, include_other_album=include_other_album, verdicts=verdicts,
                counts=counts, numbers=numbers, token=token, refused=_refusal(numbers))


def plan_view(plan: Plan) -> dict:
    """The saved_confirm.html context / JSON preview."""
    return {**plan.numbers, "selection": plan.token, "refused": plan.refused, "batch_max": BATCH_MAX}


def _batch_item(batch_id: int, pick: Pick, position: int, fmt: str) -> SavedBatchItem:
    quality, _ = request_for(pick, fmt, bulk=True)
    return SavedBatchItem(batch_id=batch_id, kind="track", saved_item_id=pick.saved_item_id, entry_id=pick.entry_id,
                          position=position, route=route_of(quality), quality=quality, state="waiting",
                          group_key=pick.group_key[:200], data=pick.to_json())


async def create_batch(session: AsyncSession, plan: Plan) -> SavedBatch:
    """Store the plan as a batch: pointers and data only, no Track row until the feeder hands a song over."""
    batch = SavedBatch(scope_text=plan.selection.scope[:300], format=plan.fmt, requested=plan.numbers["total"],
                       state="expanding" if plan.selection.pending else "active",
                       include_other_album=plan.include_other_album, counts=json.dumps(plan.numbers))
    session.add(batch)
    await session.flush()
    verdicts = iter(plan.verdicts)
    playlists: dict = {}
    quality, _ = resolve_format(plan.fmt)
    for position, unit in enumerate(plan.selection.units):
        if not isinstance(unit, Pick):
            session.add(SavedBatchItem(batch_id=batch.id, kind="collection", saved_item_id=unit.id, position=position,
                                       route=route_of(quality), quality=quality, state="expand",
                                       group_key=unit.group_key[:200]))
            continue
        pick, verdict, track = next(verdicts)
        if verdict == "queue":
            session.add(_batch_item(batch.id, pick, position, plan.fmt))
        elif pick.playlist and track is not None:
            await link_playlist(session, playlists, pick, track.id, reset=False)
    await session.commit()
    return batch


async def start_batch(session: AsyncSession, selection: Selection, fmt: str, *, include_other_album: bool,
                      token: str) -> tuple[Plan, SavedBatch | None]:
    """Under the queue lock: recount, and create the batch only when the confirmed numbers still hold."""
    async with DOWNLOAD_LOCK:
        plan = await make_plan(session, selection, fmt, include_other_album=include_other_album)
        if plan.refused or not hmac.compare_digest(plan.token, token or ""):
            return plan, None
        return plan, await create_batch(session, plan)


async def expand_into(session: AsyncSession, batch: SavedBatch, item: SavedBatchItem, picks: list[Pick]) -> None:
    """A batch reached a saved album whose songs are now read: its songs join the batch at the album's place."""
    async with DOWNLOAD_LOCK:
        d = await dedupe_context(session, picks, batch.include_other_album)
        d.seen = await batch_markers(session, batch.id)
        d.in_batches -= d.seen
        playlists: dict = {}
        for pick in picks:
            verdict, track = classify(pick, d)
            if verdict == "queue":
                session.add(_batch_item(batch.id, pick, item.position, batch.format))
                continue
            session.add(SavedBatchItem(batch_id=batch.id, kind="track", entry_id=pick.entry_id, position=item.position,
                                       state=SKIP_STATES[verdict], track_id=track.id if track else None,
                                       group_key=pick.group_key[:200], data=pick.to_json(), finished_at=utc_now()))
            if pick.playlist and track is not None:
                await link_playlist(session, playlists, pick, track.id, reset=False)
        item.state, item.note = "expanded", None
        await session.flush()
        left = await session.scalar(select(func.count(SavedBatchItem.id)).where(
            SavedBatchItem.batch_id == batch.id, SavedBatchItem.state == "expand"))
        if not left and batch.state == "expanding":
            batch.state = "active"
        await session.commit()


async def feed_item(session: AsyncSession, item: SavedBatchItem, *, as_mp3: bool) -> int | None:
    """Re-check a waiting song and create its Track row; None when it was skipped (the library has it now)."""
    async with DOWNLOAD_LOCK:
        batch = await session.get(SavedBatch, item.batch_id)
        pick = Pick.from_json(item.data)
        d = await dedupe_context(session, [pick], batch.include_other_album, batches=False)
        verdict, track = classify(pick, d)
        playlists: dict = {}
        if verdict != "queue":
            item.state, item.finished_at = SKIP_STATES[verdict], utc_now()
            item.track_id = track.id if track else None
            if pick.playlist and track is not None:
                await link_playlist(session, playlists, pick, track.id, reset=False)
            await session.commit()
            return None
        quality, container = ("mp3_320", "mp3") if as_mp3 else (item.quality, resolve_format(batch.format)[1])
        if quality == "flac_lossless":
            container = "flac"
        track = await create_row(session, pick, quality, container, existing=track)
        item.state, item.track_id, item.fed_at, item.note = "fed", track.id, utc_now(), None
        if pick.playlist:
            await link_playlist(session, playlists, pick, track.id, reset=False)
        await session.commit()
        return track.id

