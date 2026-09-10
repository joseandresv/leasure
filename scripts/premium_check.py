"""Report whether YouTube Music Premium audio actually reaches yt-dlp.

    python -m scripts.premium_check [--video ID]

Exits 0 when Premium audio is offered, 1 otherwise.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import cookies  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that YouTube Music Premium audio reaches yt-dlp.")
    parser.add_argument("--video", help="YouTube video id to probe (default: PREMIUM_CHECK_VIDEO_ID)")
    args = parser.parse_args(argv)

    result = cookies.premium_check(args.video)
    cookie = result["cookie"]

    print(f"cookie file: {cookie['path']}")
    if cookie["exists"]:
        print(f"  age: {cookie['age_hours']} h ({'stale' if cookie['stale'] else 'fresh'}), source: {cookie['source']}")
    else:
        print("  missing")

    print(f"track: https://music.youtube.com/watch?v={result['video_id']}")
    for f in result["formats"]:
        print(f"  {f['format_id']:>5}  {f['acodec']:<12} {f['abr'] or 0:>6} kbps  {f['format_note']}")

    print(f"Premium: {'PASS' if result['premium'] else 'FAIL'}")
    if result["error"]:
        print(result["error"])
    return 0 if result["premium"] else 1


if __name__ == "__main__":
    sys.exit(main())
