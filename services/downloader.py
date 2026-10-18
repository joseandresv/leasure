import asyncio
import logging
from pathlib import Path

from db import async_session
from models import Track
from services import qobuz_engine, yt_engine
from services.yt_engine import CapDeferred, DownloadFailed

logger = logging.getLogger(__name__)

# track.quality holds the request key while a row is pending.
QOBUZ_ROUTED = frozenset({"native", "flac_lossless"})
KNOWN_REQUESTS = QOBUZ_ROUTED | {"mp3_320"}
UNKNOWN_REQUEST_MESSAGE = ("This track is already downloaded or has an unknown format request; "
                           "queue it again from the Music panel.")


async def _note_skip(track_id: int, reason: str) -> None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if track:
            track.qobuz_skip_reason = reason
            await session.commit()


async def download_track(track_id: int) -> Path:
    """Qobuz first for Best and FLAC-only requests, the YouTube pipeline for the rest and as
    Best's fallback. MP3 never touches Qobuz; FLAC-only never falls back to a lossy source."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            raise DownloadFailed("This track is no longer in the library.")
        quality = track.quality
        has_source = bool(track.spotify_uri or track.youtube_id)

    if quality not in KNOWN_REQUESTS:
        raise DownloadFailed(UNKNOWN_REQUEST_MESSAGE)
    if quality in QOBUZ_ROUTED:
        try:
            return await qobuz_engine.qobuz_download(track_id)
        except qobuz_engine.QobuzSkip as skip:
            await _note_skip(track_id, skip.reason)
            if quality == "flac_lossless":
                raise DownloadFailed(str(skip)) from None
            logger.info("Track %d falls back to YouTube: Qobuz skipped (%s)", track_id, skip.reason)

    if not has_source:
        logger.error("Track %d has neither Spotify URI nor YouTube ID", track_id)
        raise DownloadFailed("This track has no Spotify or YouTube source — queue it again from the Music panel.")
    if await asyncio.to_thread(yt_engine.daily_cap_reached):
        raise CapDeferred(f"Daily YouTube download cap reached ({yt_engine.MAX_DOWNLOADS_PER_DAY} today) — "
                          "this track stays queued and resumes tomorrow.",
                          retry_after_s=yt_engine.seconds_until_tomorrow())
    return await yt_engine.download(track_id)
