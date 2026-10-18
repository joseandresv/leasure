"""Rebuild the library from the songs on an H2 card.

    python -m scripts.import_card --source /mnt/e [--dry-run] [--no-lookup] [--no-playlists]
                                  [--report PATH] [--limit-albums N]

Reads every audio file on the card (the card is never written), fills missing track and
disc numbers from Spotify, then YouTube Music, copies each song into LIBRARY_DIR under the
name Leasure gives it and records it as a finished track. Rerunning is safe: songs the
library already has are skipped, except that rows an earlier run created get the ids and
numbers a lookup that failed then (a throttled Spotify, say) finds now. The JSON report
(default data/card-import/report.json, with a .txt summary beside it) lists every decision,
including the card files that go stale after the next sync (scripts.clean_card removes them
once the sync has written their replacements); provider lookups are cached next
to it, so a rerun is cheap, and each card playlist is backed up byte for byte into
card-playlists/ beside it, since the next sync rewrites it. --dry-run does the lookups but
writes no library file and no database row. --limit-albums imports no playlists.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from db import _backup_db_file, init_db  # noqa: E402
from services.card_import import import_card  # noqa: E402


async def run(source: Path, report: Path, dry_run: bool, lookup: bool, playlists: bool, limit_albums: int | None):
    if not dry_run:
        await asyncio.to_thread(_backup_db_file)
        await init_db()
    return await import_card(source, report, dry_run=dry_run, lookup=lookup, playlists=playlists,
                             limit_albums=limit_albums)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the songs on an H2 card into the Leasure library.")
    parser.add_argument("--source", type=Path, required=True, help="the mounted card, e.g. /mnt/e")
    parser.add_argument("--dry-run", action="store_true", help="look everything up and report, but write nothing")
    parser.add_argument("--no-lookup", action="store_true", help="do not ask Spotify / YouTube Music for numbers")
    parser.add_argument("--no-playlists", action="store_true", help="do not import the card's .m3u8 playlists")
    parser.add_argument("--report", type=Path, default=settings.data_dir / "card-import" / "report.json",
                        help="where to write the JSON report (the lookup cache goes beside it)")
    parser.add_argument("--limit-albums", type=int, help="only the first N album folders, and no playlists (for a trial run)")
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        parser.error(f"{args.source} is not a directory; mount the card first")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("services.card_import").setLevel(logging.INFO)
    # Spotify answers 403 to the batch /tracks call (ISRCs) for this app; the album lookup
    # works without it, so its per-album error lines are only noise here.
    logging.getLogger("spotipy").setLevel(logging.CRITICAL)
    logging.getLogger("services.spotify_client").setLevel(logging.ERROR)
    report = asyncio.run(run(args.source, args.report, args.dry_run, not args.no_lookup, not args.no_playlists,
                             args.limit_albums))
    print(report.summary())
    print(f"Report: {args.report}")
    return 1 if report.lists.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
