"""Move the library to another folder and point every track at its new path.

    python -m scripts.move_library DEST [--dry-run] [--force]

Copies each file under LIBRARY_DIR to DEST at the same relative path, checks size and
SHA-256, and only then deletes the original (a rename cannot cross drvfs/9p mounts).
Track.file_path is rewritten for every row whose file now lives under DEST (a row whose file
could not move keeps its path), emptied folders are removed, and once nothing is left behind
the LIBRARY_DIR line for .env is printed (.env is not edited).
Rerunning after an interruption picks up where it stopped. Refuses while the Leasure
server is listening, since it may be writing into the library, unless --force.
"""

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from config import settings  # noqa: E402
from db import _backup_db_file, async_session, init_db  # noqa: E402
from models import Track  # noqa: E402
from services.card_import import copy_verified, file_sha256  # noqa: E402

COMMIT_EVERY = 25


def server_listening(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.create_connection((target, port), timeout=0.5):
            return True
    except OSError:
        return False


def _same_file(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and file_sha256(a) == file_sha256(b)


def move_files(src_root: Path, dest_root: Path, dry_run: bool) -> dict:
    """Copy-verify-delete every file under src_root into dest_root, keeping relative paths and mtimes."""
    result = {"moved": 0, "already_there": 0, "conflicts": [], "errors": []}
    for dirpath, _dirnames, filenames in os.walk(src_root):
        for name in sorted(filenames):
            src = Path(dirpath) / name
            dst = dest_root / src.relative_to(src_root)
            try:
                if dst.exists():
                    if _same_file(src, dst):
                        result["already_there"] += 1
                        if not dry_run:
                            src.unlink()
                    else:
                        result["conflicts"].append(str(src))
                    continue
                result["moved"] += 1
                if dry_run:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                copy_verified(src, dst)
                stat = src.stat()
                os.utime(dst, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                src.unlink()
            except OSError as e:
                result["errors"].append(f"{src}: {e}")
    return result


def remove_empty_dirs(root: Path) -> int:
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if Path(dirpath) == root:
            continue
        try:
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            continue
    return removed


def moved_path(file_path: str, src_root: Path, dest_root: Path) -> str | None:
    """Where a row's file lives after the move, or None when it was never under the library."""
    try:
        rel = Path(os.path.abspath(file_path)).relative_to(src_root)
    except ValueError:
        return None
    return str(dest_root / rel)


async def rewrite_paths(src_root: Path, dest_root: Path, dry_run: bool, conflicts: set[str]) -> dict:
    result = {"rewritten": 0, "outside": 0, "missing": [], "not_moved": []}
    async with async_session() as session:
        rows = (await session.scalars(select(Track).where(Track.file_path.is_not(None), Track.file_path != ""))).all()
        for row in rows:
            new = moved_path(row.file_path, src_root, dest_root)
            if new is None:
                result["outside"] += 1
                continue
            old = os.path.abspath(row.file_path)
            # A file still in the library did not move (conflict or error); DEST may hold a different file there.
            if old in conflicts or (not dry_run and await asyncio.to_thread(os.path.exists, old)):
                result["not_moved"].append(row.file_path)
                continue
            if not dry_run and not await asyncio.to_thread(os.path.exists, new):
                result["missing"].append(row.file_path)
                continue
            result["rewritten"] += 1
            if dry_run:
                continue
            row.file_path = new
            if result["rewritten"] % COMMIT_EVERY == 0:
                await session.commit()
        if not dry_run:
            await session.commit()
    return result


def _check_roots(src_root: Path, dest_root: Path) -> str | None:
    if src_root == dest_root:
        return f"The library is already at {dest_root}."
    if dest_root.is_relative_to(src_root) or src_root.is_relative_to(dest_root):
        return "DEST must not be inside the current library, nor contain it."
    return None


async def move_library(dest_root: Path, dry_run: bool) -> tuple[dict, dict]:
    src_root = Path(os.path.abspath(settings.library_dir))
    if not dry_run:
        await asyncio.to_thread(_backup_db_file)
        await init_db()
    files = await asyncio.to_thread(move_files, src_root, dest_root, dry_run)
    rows = await rewrite_paths(src_root, dest_root, dry_run, set(files["conflicts"]))
    if not dry_run:
        files["dirs_removed"] = await asyncio.to_thread(remove_empty_dirs, src_root)
    return files, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move the Leasure library to another folder.")
    parser.add_argument("dest", type=Path, help="new library folder, e.g. /mnt/c/Music/Leasure")
    parser.add_argument("--dry-run", action="store_true", help="report what would move and change nothing")
    parser.add_argument("--force", action="store_true", help="run even though the Leasure server seems to be running")
    args = parser.parse_args(argv)

    dest_root = Path(os.path.abspath(args.dest.expanduser()))
    problem = _check_roots(Path(os.path.abspath(settings.library_dir)), dest_root)
    if problem:
        print(problem)
        return 2
    if not args.force and server_listening(settings.host, settings.port):
        print(f"Leasure is running on port {settings.port}. Stop it first (it may be writing into the library), "
              "or pass --force.")
        return 2

    files, rows = asyncio.run(move_library(dest_root, args.dry_run))
    verb = "would move" if args.dry_run else "moved"
    print(f"{verb} {files['moved']} file(s), {files['already_there']} already at the destination, "
          f"{len(files['conflicts'])} conflict(s), {len(files['errors'])} error(s)")
    print(f"{'would rewrite' if args.dry_run else 'rewrote'} {rows['rewritten']} track path(s); "
          f"{rows['outside']} outside the library left alone, {len(rows['missing'])} with no file at the destination, "
          f"{len(rows['not_moved'])} left on their old path")
    for line in files["conflicts"]:
        print(f"  conflict (different file already at the destination, both kept): {line}")
    for line in files["errors"] + [f"missing: {p}" for p in rows["missing"]] + [f"not moved: {p}" for p in rows["not_moved"]]:
        print(f"  {line}")
    left_behind = files["conflicts"] or files["errors"] or rows["missing"] or rows["not_moved"]
    if left_behind and not args.dry_run:
        print("Some tracks still use the old library; resolve the lines above and rerun before changing LIBRARY_DIR.")
    elif not args.dry_run:
        print(f"Set this in .env, then restart Leasure:\n  LIBRARY_DIR={dest_root}")
    return 1 if left_behind else 0


if __name__ == "__main__":
    sys.exit(main())
