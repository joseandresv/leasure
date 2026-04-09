import logging
from pathlib import Path

from db import async_session
from models import Track

logger = logging.getLogger(__name__)


async def download_track(track_id: int) -> Path | None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

        fmt = track.quality  # mp3_320, flac_lossy, flac_lossless
        has_spotify = bool(track.spotify_uri)
        has_youtube = bool(track.youtube_id)

    if fmt == "flac_lossless":
        return await _download_lossless(track_id)

    # For non-lossless: use spotDL if we have a Spotify URI, else yt-dlp directly
    if has_spotify:
        return await _download_spotdl(track_id)
    elif has_youtube:
        return await _download_ytdlp(track_id)
    else:
        logger.error("Track %d has neither Spotify URI nor YouTube ID", track_id)
        return None


async def _download_spotdl(track_id: int) -> Path | None:
    from services.spotdl_engine import spotdl_download
    return await spotdl_download(track_id)


async def _download_ytdlp(track_id: int) -> Path | None:
    from services.ytdlp_engine import ytdlp_download
    return await ytdlp_download(track_id)


async def _download_lossless(track_id: int) -> Path | None:
    from services.streamrip_engine import streamrip_download
    from services.bandcamp_engine import bandcamp_download
    from services.archive_engine import archive_download

    for engine_fn, engine_name in [
        (streamrip_download, "streamrip"),
        (bandcamp_download, "bandcamp"),
        (archive_download, "archive"),
    ]:
        try:
            result = await engine_fn(track_id)
            if result:
                async with async_session() as session:
                    track = await session.get(Track, track_id)
                    track.engine_used = engine_name
                    await session.commit()
                return result
        except Exception as e:
            logger.warning("Engine %s failed for track %d: %s", engine_name, track_id, e)

    # Fallback: use spotDL or yt-dlp for lossy FLAC
    logger.info("All lossless engines failed for track %d, falling back", track_id)
    async with async_session() as session:
        track = await session.get(Track, track_id)
        track.quality = "flac_lossy"
        track.engine_used = "fallback"
        await session.commit()
        has_spotify = bool(track.spotify_uri)

    if has_spotify:
        return await _download_spotdl(track_id)
    return await _download_ytdlp(track_id)
