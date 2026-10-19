"""Remove the card files a card import left stale, once the sync has written their replacements.

    python -m scripts.clean_card --card /mnt/e [--report PATH] [--apply]

After `scripts.import_card` and a sync, songs whose name gained a track number (or moved to
the album artist's folder) sit on the card twice: the H2 scans the whole card, so it lists
them twice. This deletes each file of the report's stale_card_copies, with its .jpg/.lrc,
only when the library track that replaces it is on the card at its new path with the library
file's size and no library track syncs to the old path. Without --apply it only lists what it
would delete. The card must be a volume Leasure detects; nothing else on it is touched (never
anything under /.rockbox or the OS's system folders), but playlists Leasure does not manage that
name a removed file are listed (they will miss it).
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from services.card_import import clean_stale_card_copies  # noqa: E402


def summary(result: dict) -> str:
    verb = "removed" if result["applied"] else "would remove"
    lines = [f"{verb} {len(result['removed'])} stale card files, kept {len(result['kept'])}, "
             f"already gone {len(result['already_gone'])}, errors {len(result['errors'])}"]
    for reason in sorted({k["reason"] for k in result["kept"]}):
        lines.append(f"  kept {sum(k['reason'] == reason for k in result['kept'])}: {reason}")
    if result["playlists_naming_removed"]:
        lines.append(f"  playlists that list a removed file: {', '.join(result['playlists_naming_removed'])}")
    if not result["applied"]:
        lines.append("  dry run: nothing deleted; rerun with --apply")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete the card files a card import made stale, after a sync.")
    parser.add_argument("--card", required=True, help="the mounted card, e.g. /mnt/e")
    parser.add_argument("--report", type=Path, default=settings.data_dir / "card-import" / "report.json",
                        help="the import report that lists stale_card_copies")
    parser.add_argument("--apply", action="store_true", help="delete; without it only list what would go")
    args = parser.parse_args(argv)

    if not args.report.is_file():
        parser.error(f"{args.report} not found; run scripts.import_card first")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        result = asyncio.run(clean_stale_card_copies(args.card, args.report, apply=args.apply))
    except ValueError as e:
        parser.error(str(e))
    out = args.report.with_name("clean-card.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summary(result))
    print(f"Details: {out}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
