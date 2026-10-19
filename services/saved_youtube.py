"""Reads the YouTube Music library (liked songs, albums, playlists, subscriptions) into the Saved snapshot.
Stale cookies can answer short or empty, so a removal always needs two passes that agree."""

import asyncio
import logging

from sqlalchemy import or_, select

from models import utc_now
from models_saved import SavedItem
from services import youtube_client
from services.saved_store import (
    SavedData,
    Stopped,
    active_ids,
    replace_entries,
    run_step,
    set_state,
    sweep,
    sync_state,
    unseen_since,
    upsert_items,
)

logger = logging.getLogger(__name__)

SOURCE = "youtube"
SHORTFALL = 0.9
PLAYLISTS_PER_RUN = 10
# Liked Music and Saved Episodes are not playlists of their own here.
SKIP_PLAYLISTS = frozenset({"LM", "SE"})


def _int(value) -> int | None:
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


def track_data(t: dict) -> SavedData:
    return SavedData(source_id=t["id"], title=t.get("name") or "", artist=t.get("artist") or None, album=t.get("album"),
                     album_source_id=t.get("album_id"), album_artist=t.get("album_artist"), image_url=t.get("image_url"),
                     duration_ms=t.get("duration_ms") or None, track_number=t.get("track_number"),
                     total_tracks=t.get("total_tracks"))


def album_data(a: dict) -> SavedData:
    return SavedData(source_id=a["id"], title=a.get("name") or "", artist=a.get("artist") or None,
                     album_artist=a.get("artist") or None, image_url=a.get("image_url"), year=_int(str(a.get("year") or "")[:4]))


def playlist_data(p: dict) -> SavedData:
    count = _int(p.get("count"))
    return SavedData(source_id=p["id"], title=p.get("name") or "", image_url=p.get("image_url"), track_count=count,
                     snapshot_tag=str(count) if count is not None else None, readable=None)


def artist_data(a: dict) -> SavedData:
    return SavedData(source_id=a["id"], title=a.get("name") or "", image_url=a.get("image_url"))


FETCHERS = {
    "track": (lambda: youtube_client.get_liked_songs(limit=None), track_data),
    "album": (lambda: youtube_client.get_library_albums(limit=None, order="recently_added"), album_data),
    "playlist": (lambda: youtube_client.get_playlists(limit=None), playlist_data),
    "artist": (lambda: youtube_client.get_library_subscriptions(limit=None), artist_data),
}


def _unique(items: list[SavedData]) -> list[SavedData]:
    seen: set[str] = set()
    return [d for d in items if not (d.source_id in seen or seen.add(d.source_id))]


async def read_kind(session_factory, kind: str) -> None:
    """A full pass of one kind. A short answer is checked against the connection before anything is swept."""
    fetch, parse = FETCHERS[kind]
    rows = await asyncio.to_thread(fetch)
    if rows is None:
        raise Stopped("no_answer", state="error", message="YouTube Music did not answer")
    items = _unique([parse(row) for row in rows if row.get("id") and row["id"] not in SKIP_PLAYLISTS])
    async with session_factory() as session:
        previous = len(await active_ids(session, SOURCE, kind))
    if previous and len(items) < SHORTFALL * previous and not await asyncio.to_thread(youtube_client.is_connected):
        raise Stopped("not_connected", state="blocked")
    started = utc_now()
    async with session_factory() as session:
        await upsert_items(session, SOURCE, kind, items, now=started)
        state = await sync_state(session, SOURCE, kind)
        await sweep(session, state, set((await unseen_since(session, SOURCE, kind, started)).values()),
                    now=utc_now(), always_two_pass=True)
        state.last_full_at, state.total_remote, state.done = utc_now(), len(items), len(items)
        await session.commit()
    if kind == "playlist":
        await _playlist_entries(session_factory)


async def _playlist_entries(session_factory) -> None:
    """Tracks of up to PLAYLISTS_PER_RUN playlists whose count changed (or is unknown), least recently read first."""
    async with session_factory() as session:
        due = (await session.scalars(select(SavedItem).where(
            SavedItem.source == SOURCE, SavedItem.kind == "playlist", SavedItem.removed_at.is_(None),
            or_(SavedItem.snapshot_tag.is_(None), SavedItem.entries_tag.is_(None),
                SavedItem.entries_tag != SavedItem.snapshot_tag))
            .order_by(SavedItem.entries_fetched_at, SavedItem.position).limit(PLAYLISTS_PER_RUN))).all()
    for item in due:
        playlist = await asyncio.to_thread(youtube_client.get_playlist_tracks, item.source_id, None)
        if playlist is None:
            continue
        async with session_factory() as session:
            row = await session.get(SavedItem, item.id)
            await replace_entries(session, row, [track_data(t) for t in playlist["tracks"]], tag=row.snapshot_tag,
                                  now=utc_now())
            await session.commit()


async def run(session_factory) -> None:
    """Every run is a full pass of each kind."""
    if not youtube_client.HEADERS_PATH.exists():
        for kind in FETCHERS:
            await set_state(session_factory, SOURCE, kind, state="blocked", blocked_reason="not_connected",
                            last_run_at=utc_now())
        return
    for kind in FETCHERS:
        if not await run_step(session_factory, SOURCE, kind, lambda k=kind: read_kind(session_factory, k)):
            return
