import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track

logger = logging.getLogger(__name__)


async def streamrip_download(track_id: int) -> Path | None:
    if not any([settings.qobuz_email, settings.tidal_email, settings.deezer_arl]):
        logger.info("No lossless service configured, skipping streamrip")
        return None

    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

    # TODO: Implement streamrip integration
    # This will search Qobuz/Tidal/Deezer by artist + title + album
    # and download the best available FLAC quality
    logger.info(
        "streamrip engine not yet implemented — would search for '%s' by '%s'",
        track.title,
        track.artist,
    )
    return None
