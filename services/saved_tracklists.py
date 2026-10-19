"""A saved album's tracklist, read from its service the first time it is opened or downloaded."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models import utc_now
from models_saved import SavedItem
from services import (
    qobuz_client,
    qobuz_engine,
    qobuz_session,
    saved_qobuz,
    saved_spotify,
    saved_youtube,
    spotify_albums,
    youtube_client,
)
from services import spotify_client as sp
from services.saved_store import SavedData, replace_entries

logger = logging.getLogger(__name__)


def _qobuz_album(album_id: str) -> tuple[list[SavedData] | None, str | None]:
    """Blocking: the album's tracklist from Qobuz (one call), or a notice why not."""
    stored = qobuz_session.usable()
    if stored is None:
        return None, "Qobuz sign-in needed to read this album."
    if qobuz_engine.cooldown_left() or qobuz_engine.breaker_state()["reason"]:
        return None, "Qobuz is pausing Leasure right now; open this album again later."
    try:
        with qobuz_session.make_client(stored) as client:
            _album, tracks = client.get_album(album_id)
            if client.rotated_token:
                qobuz_session.store_rotated_token(qobuz_session.token_fingerprint(stored.user_auth_token),
                                                  client.rotated_token)
    except (qobuz_client.QobuzError, qobuz_session.SessionChanged) as e:
        logger.info("Qobuz album %s could not be read: %s", album_id, type(e).__name__)
        return None, "Qobuz did not return this album's songs; try again later."
    return [saved_qobuz.track_data(t) for t in tracks], None


def _spotify_budget_left() -> bool:
    used = sp.calls_today()
    return used["sync"] < saved_spotify.DAILY_SYNC and used["total"] < saved_spotify.DAILY_TOTAL


def _spotify_album(item: SavedItem, background: bool = False) -> tuple[list[SavedData] | None, str | None]:
    if background:
        if not _spotify_budget_left():
            return None, "Leasure has used today's Spotify allowance for library reading."
        with sp.counting_as_sync():
            data = spotify_albums.get_album(item.source_id)
    else:
        data = spotify_albums.get_album(item.source_id)
    if not data:
        return None, sp.rate_limit_message() or "Spotify did not return this album's songs; try again later."
    return [saved_spotify.track_data({**t, "album": item.title, "album_id": item.source_id,
                                      "album_image_url": item.image_url}) for t in data["tracks"]], None


def _youtube_album(item: SavedItem) -> tuple[list[SavedData] | None, str | None]:
    data = youtube_client.get_album_tracks(item.source_id)
    if not data:
        return None, "YouTube Music did not return this album's songs; try again later."
    return [saved_youtube.track_data({**t, "album": item.title, "image_url": item.image_url}) for t in data["tracks"]], None


async def ensure_album_entries(session: AsyncSession, item: SavedItem, *, background: bool = False) -> str | None:
    """A saved album's tracklist is read the first time it is opened or downloaded; returns a notice when it cannot
    be. `background` (the Download all feeder) reads Spotify only within the library-reading share of the day."""
    if item.kind != "album" or item.entries_fetched_at is not None:
        return None
    if item.source == "qobuz":
        entries, notice = await asyncio.to_thread(_qobuz_album, item.source_id)
    elif item.source == "spotify":
        entries, notice = await asyncio.to_thread(_spotify_album, item, background)
    else:
        entries, notice = await asyncio.to_thread(_youtube_album, item)
    if entries is not None:
        await replace_entries(session, item, entries, tag=None, now=utc_now())
        await session.commit()
    return notice
