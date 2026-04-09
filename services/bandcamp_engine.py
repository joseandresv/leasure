import logging
from pathlib import Path

from db import async_session
from models import Track

logger = logging.getLogger(__name__)


async def bandcamp_download(track_id: int) -> Path | None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

    # TODO: Implement Bandcamp search + download
    # Search bandcamp for matching artist/album, download FLAC if available
    logger.info(
        "bandcamp engine not yet implemented — would search for '%s' by '%s'",
        track.title,
        track.artist,
    )
    return None
