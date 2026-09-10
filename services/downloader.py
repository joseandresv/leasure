import logging
from pathlib import Path

from db import async_session
from models import Track
from services.yt_engine import DownloadFailed, download

logger = logging.getLogger(__name__)


async def download_track(track_id: int) -> Path:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            raise DownloadFailed("This track is no longer in the library.")

        quality = track.quality
        has_source = bool(track.spotify_uri or track.youtube_id)

    if quality == "flac_lossless":
        return await _download_lossless(track_id)

    if not has_source:
        logger.error("Track %d has neither Spotify URI nor YouTube ID", track_id)
        raise DownloadFailed("This track has no Spotify or YouTube source — queue it again from the Music panel.")

    return await download(track_id)


async def _download_lossless(track_id: int) -> Path:
    from services.archive_engine import archive_download
    from services.bandcamp_engine import bandcamp_download
    from services.streamrip_engine import streamrip_download

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
                    track.is_lossless = True
                    await session.commit()
                return result
        except Exception as e:
            logger.warning("Engine %s failed for track %d: %s", engine_name, track_id, e)

    # No silent lossy substitute: a FLAC container filled from a YouTube stream would
    # claim a fidelity the source never had.
    raise DownloadFailed(
        "No lossless source available — configure Qobuz/Tidal/Deezer, or download as "
        "Best available / MP3 instead."
    )
