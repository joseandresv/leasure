"""The one place a provider track dict becomes a queued Track row."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models import Track

logger = logging.getLogger(__name__)

# Identity columns: only ever written from a truthy value, so looking a track up by
# one source never wipes the other source's id.
_IDENTITY_FIELDS = ("spotify_uri", "youtube_id", "isrc")

_METADATA_FIELDS = (
    "title", "artist", "album", "album_artist", "genre", "track_number", "disc_number",
    "total_tracks", "total_discs", "duration_ms", "year", "artwork_url",
)


def optional_int(value) -> int | None:
    """Parse an optional numeric form field. hx-vals sends "" for a value the page does
    not have, which a typed `int | None` parameter would reject with a 422."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def upsert_track(
    session: AsyncSession,
    data: dict,
    *,
    quality: str,
    container: str,
    source: str,
    existing: Track | None = None,
) -> Track:
    """Create or refresh the row for `data` and leave it queued (status 'pending').

    Keys missing from `data` leave the column alone, so re-queueing a track after an
    error keeps metadata an earlier, richer fetch had already stored."""
    track = existing or Track()
    for field in _IDENTITY_FIELDS:
        if data.get(field):
            setattr(track, field, data[field])
    for field in _METADATA_FIELDS:
        value = data.get(field)
        if value is not None:
            setattr(track, field, value)
    track.format = container
    track.quality = quality
    track.source = source
    track.status = "pending"
    track.error_message = None

    if existing is None:
        session.add(track)
    await session.commit()
    await session.refresh(track)
    return track
