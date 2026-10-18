"""The one place a provider track dict becomes a queued Track row."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from models import Track
from services import tagger, track_locks
from services.device import build_device_path
from services.yt_engine import _files_of_other_tracks, _track_stem, with_audio_suffix

logger = logging.getLogger(__name__)

# Identity columns: only ever written from a truthy value, so looking a track up by
# one source never wipes the other source's id.
_IDENTITY_FIELDS = ("spotify_uri", "youtube_id", "isrc")

_ALBUM_FIELDS = ("track_number", "disc_number", "total_tracks", "total_discs", "album", "album_artist")
_SIDECAR_SUFFIXES = (".jpg", ".lrc")

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


def refresh_album_fields(track: Track, data: dict) -> bool:
    """Copy album numbering/naming from `data` onto `track`; True when anything changed."""
    changed = False
    for field in _ALBUM_FIELDS:
        value = data.get(field)
        if value is not None and getattr(track, field) != value:
            setattr(track, field, value)
            changed = True
    return changed


def relocate_library_file(current: Path, stem: Path, taken: frozenset[str] = frozenset()) -> Path | None:
    """Move a library file and its .jpg/.lrc sidecars to `stem` (same extension) and
    return where the audio now lives, or None when it is missing. Never overwrites
    another file; a failed move is undone. Thread-side."""
    dest = with_audio_suffix(stem, current.suffix)
    if not current.exists():
        # An earlier move whose DB write was lost: adopt the file unless another row owns it.
        return dest if dest.exists() and str(dest) not in taken else None
    if dest == current:
        return current
    if dest.exists() and not os.path.samefile(dest, current):
        logger.warning("Not moving %s: %s already exists", current, dest)
        return current
    dest.parent.mkdir(parents=True, exist_ok=True)
    moves = [(current, dest)] + [(current.with_suffix(suffix), dest.with_suffix(suffix)) for suffix in _SIDECAR_SUFFIXES]
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            if src.exists() and not dst.exists():
                os.replace(src, dst)
                done.append((src, dst))
    except OSError:
        for src, dst in reversed(done):
            os.replace(dst, src)
        raise
    # Best effort: the old album folder goes only if the move left it empty.
    with contextlib.suppress(OSError):
        current.parent.rmdir()
    return dest


def _card_path(track: Track) -> str:
    return build_device_path(track.artist, track.album, track.track_number, track.title,
                             Path(track.file_path).suffix.lstrip("."), album_artist=track.album_artist,
                             disc_number=track.disc_number, total_discs=track.total_discs)


async def refresh_album_track(session: AsyncSession, track: Track, data: dict) -> str | None:
    """An album download met a track the library already has: store the album's numbering
    and, for a finished download, rename the file to match and retag it.

    Returns the card path a synced track had before when it changed: sync never deletes
    audio, so that old copy stays on the card next to the new one."""
    async with track_locks.hold(track.id):
        # A FLAC upgrade or revert may have swapped the file while this waited for the lock.
        await session.refresh(track)
        old_card_path = _card_path(track) if track.synced_at and track.file_path else None
        if not refresh_album_fields(track, data):
            return None
        old_file, path = track.file_path, None
        if track.status == "done" and track.file_path:
            stem = _track_stem(track)
            path = await asyncio.to_thread(relocate_library_file, Path(track.file_path), stem,
                                           await _files_of_other_tracks(track.id, stem))
            if path:
                track.file_path = str(path)
            track.synced_at = None
        try:
            await session.commit()
        except Exception:
            if path and str(path) != old_file:
                old = Path(old_file)
                await asyncio.to_thread(relocate_library_file, path, old.with_name(old.stem))
            raise
    stale = old_card_path if old_card_path and old_card_path != _card_path(track) else None
    if stale:
        logger.info("Track %d now syncs to a new card path; its old copy %s stays on the card", track.id, stale)
    if path is not None:
        try:
            await tagger.apply_full_metadata(path, track.id)
        except Exception:
            logger.exception("Retagging track %d after the album refresh failed", track.id)
    return stale
