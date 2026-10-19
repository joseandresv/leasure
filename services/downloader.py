import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from db import async_session
from models import Track
from models_saved import SavedBatchItem
from services import qobuz_engine, yt_engine
from services.yt_engine import SEARCHABLE_SOURCES, CapDeferred, DownloadFailed

logger = logging.getLogger(__name__)

# track.quality holds the request key while a row is pending.
QOBUZ_ROUTED = frozenset({"native", "flac_lossless"})
KNOWN_REQUESTS = QOBUZ_ROUTED | {"mp3_320"}
UNKNOWN_REQUEST_MESSAGE = ("This track is already downloaded or has an unknown format request; "
                           "queue it again from the Music panel.")
# "Download all" rows wait for Qobuz on these instead of falling back to YouTube.
BULK_WAIT_REASONS = {"cap": None, "unavailable": qobuz_engine.BREAKER_OPEN_S,
                     "cdn_unavailable": qobuz_engine.BREAKER_OPEN_S, "signed_out": qobuz_engine.BREAKER_OPEN_S}
# Of YT_MAX_DOWNLOADS_PER_DAY, what "Download all" leaves for clicks.
BULK_YT_RESERVE = 10


async def _note_skip(track_id: int, reason: str) -> None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if track:
            track.qobuz_skip_reason = reason
            await session.commit()


async def is_bulk(track_id: int) -> bool:
    """Whether the row was handed over by a "Download all" batch (survives a restart)."""
    async with async_session() as session:
        return await session.scalar(select(SavedBatchItem.id).where(
            SavedBatchItem.track_id == track_id, SavedBatchItem.state == "fed").limit(1)) is not None


async def _request_mp3(track_id: int) -> None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if track:
            track.quality, track.format = "mp3_320", "mp3"
            await session.commit()


async def _bulk_fallback(track_id: int, skip: qobuz_engine.QobuzSkip) -> None:
    """A "Download all" row falls back only when Qobuz lacks the song or cannot be used at all, and then
    as MP3 320 (the H2 plays no AAC); a cap, breaker or expired sign-in keeps it waiting for Qobuz."""
    if skip.reason in BULK_WAIT_REASONS:
        wait = BULK_WAIT_REASONS[skip.reason] or yt_engine.seconds_until_tomorrow()
        raise CapDeferred(f"{skip} This Download all song waits for Qobuz.", retry_after_s=wait)
    await _request_mp3(track_id)


def bulk_youtube_room_left() -> bool:
    return yt_engine.downloads_today() < yt_engine.MAX_DOWNLOADS_PER_DAY - BULK_YT_RESERVE


async def download_track(track_id: int) -> Path:
    """Qobuz first for Best and FLAC-only requests, the YouTube pipeline for the rest and as
    Best's fallback. MP3 never touches Qobuz; FLAC-only never falls back to a lossy source."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            raise DownloadFailed("This track is no longer in the library.")
        quality = track.quality
        has_source = bool(track.spotify_uri or track.youtube_id) or (
            track.source in SEARCHABLE_SOURCES and bool(track.title and track.artist))

    if quality not in KNOWN_REQUESTS:
        raise DownloadFailed(UNKNOWN_REQUEST_MESSAGE)
    bulk = await is_bulk(track_id)
    if quality in QOBUZ_ROUTED:
        try:
            return await qobuz_engine.qobuz_download(track_id, kind="bulk" if bulk else "interactive")
        except qobuz_engine.QobuzSkip as skip:
            await _note_skip(track_id, skip.reason)
            if quality == "flac_lossless":
                raise DownloadFailed(str(skip)) from None
            if bulk:
                await _bulk_fallback(track_id, skip)
            logger.info("Track %d falls back to YouTube: Qobuz skipped (%s)", track_id, skip.reason)

    if not has_source:
        logger.error("Track %d has neither Spotify URI nor YouTube ID", track_id)
        raise DownloadFailed("This track has no Spotify or YouTube source — queue it again from the Music panel.")
    if bulk and not await asyncio.to_thread(bulk_youtube_room_left):
        raise CapDeferred("Today's YouTube downloads for Download all are used up — this song resumes tomorrow.",
                          retry_after_s=yt_engine.seconds_until_tomorrow())
    if await asyncio.to_thread(yt_engine.daily_cap_reached):
        raise CapDeferred(f"Daily YouTube download cap reached ({yt_engine.MAX_DOWNLOADS_PER_DAY} today) — "
                          "this track stays queued and resumes tomorrow.",
                          retry_after_s=yt_engine.seconds_until_tomorrow())
    return await yt_engine.download(track_id)
