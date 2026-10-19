"""Which saved songs the library already has: live for the page on screen, and cached for the whole
snapshot by a chunked background pass (the "Not downloaded yet" filter and the header counts read the cache)."""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import Track, utc_now
from models_saved import SavedEntry, SavedItem
from services import library_match, local_albums
from services.album_merge import ef

logger = logging.getLogger(__name__)

REMATCH_CHUNK = 500
STATE_RANK = {"library": 4, "queued": 3, "other_album": 2, "error": 1}
OWNED_STATES = ("library", "other_album")


@dataclass(frozen=True)
class Match:
    state: str          # library | other_album | queued | error
    track: Track


def _keys(source: str, row) -> library_match.TrackKeys:
    uri = row.uri or ""
    return library_match.TrackKeys(
        title=row.title or "", artist=row.artist or "", album=row.album or "",
        youtube_id=row.source_id if source == "youtube" else None,
        spotify_uri=uri if source == "spotify" and uri.startswith("spotify:track:") else None,
        isrc=row.isrc, qobuz_id=row.source_id if source == "qobuz" else None)


def _state(track: Track, *, other_album: bool) -> str:
    if track.status == "done":
        return "other_album" if other_album else "library"
    return "error" if track.status == "error" else "queued"


async def match_rows(session: AsyncSession, rows: list[tuple[str, object]]) -> list[Match | None]:
    """The library row of each (source, saved item or entry): by id, ISRC or song on the same album, then the
    same song on any other album (find_song)."""
    if not rows:
        return []
    keys = [_keys(source, row) for source, row in rows]
    index = await library_match.library_index(session, keys)
    found: list[Match | None] = []
    for key in keys:
        hit = index.find(key)
        found.append(Match(_state(hit, other_album=False), hit) if hit else None)
    misses = [i for i, match in enumerate(found) if match is None and rows[i][1].title]
    if misses:
        songs = await library_match.song_index(session, {ef(rows[i][1].title) for i in misses})
        for i in misses:
            row = rows[i][1]
            hit = songs.find_song(row.title, {row.artist or "", row.album_artist or ""}, row.duration_ms)
            found[i] = Match(_state(hit, other_album=True), hit) if hit else None
    return found


def best(matches: list[Match | None]) -> Match | None:
    present = [m for m in matches if m is not None]
    return max(present, key=lambda m: STATE_RANK[m.state]) if present else None


async def _rematch_chunk(session: AsyncSession, rows: list[tuple[str, object]], now) -> None:
    for (_source, row), match in zip(rows, await match_rows(session, rows), strict=True):
        row.match_track_id = match.track.id if match else None
        row.match_state = match.state if match else None
        row.matched_at = now


async def _album_from_library(session: AsyncSession, item: SavedItem, albums: list[local_albums.LocalAlbum]) -> int | None:
    """Done songs of the local album a saved album is (None when the library has no such album)."""
    found = await local_albums.find_key(session, item.title, item.artist or "", albums=albums)
    if found is None:
        return None
    return next((a.track_count for a in albums if a.key == found[0]), None)


async def _rematch_collections(session: AsyncSession, now) -> None:
    owned = func.sum(case((SavedEntry.match_state.in_(OWNED_STATES), 1), else_=0))
    counts = {cid: (n, have or 0) for cid, n, have in await session.execute(
        select(SavedEntry.collection_id, func.count(SavedEntry.id), owned).group_by(SavedEntry.collection_id))}
    items = (await session.scalars(select(SavedItem).where(SavedItem.kind.in_(("album", "playlist"))))).all()
    # One library snapshot, read before the first assignment: a query after it would autoflush an UPDATE and
    # hold SQLite's write lock for the rest of the loop.
    albums = await local_albums.scan(session)
    with session.no_autoflush:
        for item in items:
            total, have = counts.get(item.id, (0, None))
            if not total and item.kind == "album":
                have, total = await _album_from_library(session, item, albums), item.track_count or 0
            item.entries_matched = have
            item.match_state = "library" if total and have is not None and have >= total else None
            item.matched_at = now


async def rematch_all(session_factory: async_sessionmaker) -> None:
    """Refresh the cached match of every saved song, entry and collection, one short transaction per chunk."""
    now = utc_now()
    for model in (SavedItem, SavedEntry):
        last_id = 0
        while True:
            async with session_factory() as session:
                stmt = select(model, SavedItem.source)
                if model is SavedEntry:
                    stmt = stmt.join(SavedItem, SavedItem.id == SavedEntry.collection_id)
                else:
                    stmt = stmt.where(SavedItem.kind == "track")
                chunk = (await session.execute(stmt.where(model.id > last_id).order_by(model.id)
                                               .limit(REMATCH_CHUNK))).all()
                if not chunk:
                    break
                await _rematch_chunk(session, [(source, row) for row, source in chunk], now)
                await session.commit()
                last_id = chunk[-1][0].id
            await asyncio.sleep(0)
    async with session_factory() as session:
        await _rematch_collections(session, now)
        await session.commit()


async def library_signature(session: AsyncSession) -> tuple:
    """Changes whenever a song is added, removed, finishes downloading or changes state."""
    row = (await session.execute(select(
        func.count(Track.id), func.max(Track.id), func.max(Track.downloaded_at),
        func.sum(case((Track.status == "done", 1), else_=0)),
        func.sum(case((Track.status == "error", 1), else_=0))))).one()
    return tuple(row)
