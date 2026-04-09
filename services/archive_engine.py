import logging
from pathlib import Path

from db import async_session
from models import Track

logger = logging.getLogger(__name__)


async def archive_download(track_id: int) -> Path | None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

    # TODO: Implement Internet Archive search + download
    # Query archive.org advanced search for FLAC audio matching artist/title
    logger.info(
        "archive engine not yet implemented — would search for '%s' by '%s'",
        track.title,
        track.artist,
    )
    return None
