"""Backfill provenance on tracks downloaded before the engine recorded any.

    python -m scripts.reprobe [--dry-run]

Only rows with status 'done' and no source_format_id are touched. What the file
really came from is unknowable after the fact, so this records what the container
still proves (transcoded or not, sample rate) and marks the rest 'unverified'
rather than guessing a bitrate.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from db import _backup_db_file, async_session, init_db  # noqa: E402
from models import Track  # noqa: E402

logger = logging.getLogger(__name__)

# A lossy source in one of these containers can only have been re-encoded.
_TRANSCODED_BY_SUFFIX = {".mp3": True, ".flac": True, ".wav": True,
                         ".m4a": False, ".opus": False, ".ogg": False, ".webm": False}
# Legacy "native" rows recorded the request, not the result; name the family only.
_NATIVE_QUALITY = {".m4a": "aac_unknown", ".opus": "opus_unknown", ".ogg": "vorbis_unknown", ".mp3": "mp3_unknown"}
_COMMIT_EVERY = 25


def _sample_rate(path: Path) -> int | None:
    import mutagen

    try:
        audio = mutagen.File(path)
    except Exception as e:
        logger.warning("Could not read %s: %s", path, e)
        return None
    return getattr(getattr(audio, "info", None), "sample_rate", None)


def _suffix(track: Track) -> str:
    return Path(track.file_path).suffix.lower() if track.file_path else ""


def _plan(track: Track, suffix: str, sample_rate: int | None) -> dict:
    """The provenance fields this row should end up with."""
    transcoded = _TRANSCODED_BY_SUFFIX.get(suffix, track.transcoded)
    if track.quality == "flac_lossy":
        transcoded = True
    plan = {
        "quality": _NATIVE_QUALITY.get(suffix, "native") if track.quality == "native" else track.quality,
        "transcoded": transcoded,
        "is_lossless": False,
        "verification": "unverified",
    }
    if sample_rate and not track.source_sample_rate:
        plan["source_sample_rate"] = sample_rate
    return plan


async def backfill(dry_run: bool) -> dict:
    counts = {"scanned": 0, "relabelled": 0, "sample_rates": 0, "no_file": 0}
    if not dry_run:
        await asyncio.to_thread(_backup_db_file)
    # The rows this touches live in databases written before the provenance columns
    # existed, so bring the schema up to the models first (additive, same as startup).
    await init_db()

    async with async_session() as session:
        rows = (await session.execute(
            select(Track).where(Track.status == "done", Track.source_format_id.is_(None))
        )).scalars().all()

        for position, track in enumerate(rows, 1):
            counts["scanned"] += 1
            suffix = _suffix(track)
            path = Path(track.file_path) if track.file_path else None
            sample_rate = None
            if path and path.exists():
                sample_rate = await asyncio.to_thread(_sample_rate, path)
            else:
                counts["no_file"] += 1

            plan = _plan(track, suffix, sample_rate)
            if plan["quality"] != track.quality:
                counts["relabelled"] += 1
            if "source_sample_rate" in plan:
                counts["sample_rates"] += 1

            print(f"{track.id:>6}  {suffix or '?':<6} {track.quality or '?':<14} -> {plan['quality'] or '?':<14} "
                  f"transcoded={plan['transcoded']} sample_rate={plan.get('source_sample_rate') or '-'}")

            if dry_run:
                continue
            for field, value in plan.items():
                setattr(track, field, value)
            if position % _COMMIT_EVERY == 0:
                await session.commit()

        if not dry_run:
            await session.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill provenance on pre-provenance downloads.")
    parser.add_argument("--dry-run", action="store_true", help="report what would change and write no row")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    counts = asyncio.run(backfill(args.dry_run))
    print(f"{'would update' if args.dry_run else 'updated'} {counts['scanned']} row(s): "
          f"{counts['relabelled']} quality relabelled, {counts['sample_rates']} sample rate(s) read, "
          f"{counts['no_file']} without a readable file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
