"""Re-tag files downloaded before the Sprint 2 tag writer.

    python -m scripts.retag [--dry-run] [--only-missing] [--ids 1,2,3]

Runs services.tagger.apply_full_metadata over every finished track, so each file gets
ID3v2.3, "n/total" track numbers, an explicit album artist, centre-cropped art and
synced-only lyrics. Rows with no genre also get one from MusicBrainz, which is throttled
to one request every 1.1 s -- expect roughly that per untagged row.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select  # noqa: E402

from db import _backup_db_file, async_session, init_db  # noqa: E402
from models import Track  # noqa: E402
from services.tagger import apply_full_metadata  # noqa: E402

logger = logging.getLogger(__name__)


async def _rows_to_retag(only_missing: bool, ids: list[int] | None) -> list[dict]:
    """The tracks to re-tag, as plain dicts so no ORM object outlives its session."""
    stmt = select(Track).where(Track.status == "done", Track.file_path.is_not(None), Track.file_path != "")
    if ids:
        stmt = stmt.where(Track.id.in_(ids))
    if only_missing:
        stmt = stmt.where(or_(Track.album_artist.is_(None), Track.album_artist == "",
                              Track.genre.is_(None), Track.genre == ""))
    async with async_session() as session:
        rows = (await session.execute(stmt.order_by(Track.id))).scalars().all()
    return [{"id": t.id, "path": Path(t.file_path), "artist": t.artist, "title": t.title} for t in rows]


async def retag(dry_run: bool, only_missing: bool, ids: list[int] | None) -> dict:
    counts = {"scanned": 0, "tagged": 0, "no_file": 0, "errors": 0}
    if not dry_run:
        await asyncio.to_thread(_backup_db_file)
    await init_db()

    rows = await _rows_to_retag(only_missing, ids)
    total = len(rows)
    for position, row in enumerate(rows, 1):
        counts["scanned"] += 1
        label = f"[{position}/{total}] {row['id']:>6}  {row['artist'] or '?'} - {row['title'] or '?'}"
        if not row["path"].exists():
            counts["no_file"] += 1
            print(f"{label}  skipped, file is gone: {row['path']}")
            continue

        print(label, flush=True)
        if dry_run:
            continue
        try:
            await apply_full_metadata(row["path"], row["id"])
        except Exception as e:
            counts["errors"] += 1
            logger.warning("Track %s could not be re-tagged: %s", row["id"], str(e)[:200])
            continue
        counts["tagged"] += 1
    return counts


def _parse_ids(value: str) -> list[int]:
    try:
        return [int(part) for part in value.split(",") if part.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--ids takes a comma-separated list of track ids: {e}") from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-tag downloaded files with the current tag writer.")
    parser.add_argument("--dry-run", action="store_true", help="list what would be re-tagged and write nothing")
    parser.add_argument("--only-missing", action="store_true",
                        help="only rows with no album artist or no genre")
    parser.add_argument("--ids", type=_parse_ids, help="limit to these track ids, comma-separated")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    counts = asyncio.run(retag(args.dry_run, args.only_missing, args.ids))
    handled = counts["scanned"] - counts["no_file"] if args.dry_run else counts["tagged"]
    print(f"{'would re-tag' if args.dry_run else 're-tagged'} {handled} of {counts['scanned']} row(s): "
          f"{counts['no_file']} without a file, {counts['errors']} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
