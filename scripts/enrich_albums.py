"""Prefetch album info (MusicBrainz, then Deezer) for every album in the library, once.

    python -m scripts.enrich_albums [--apply] [--limit N] [--force] [--key KEY]
                                    [--source musicbrainz|deezer|any] [--prune]

Without --apply it is a read-only dry run: the database is opened read-only, nothing is
requested, and it lists which albums are found, not found (and when they retry), failed,
due or no longer matching. --apply looks the due albums up one by one under the usual rate
limits (MusicBrainz 1.5 s here, since the server's genre lookups may run alongside), saving
each result as it goes; it holds data/album_info.lock so the server queues no lookups
meanwhile. --prune (with --apply) deletes stored info of albums no longer in the library.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import db  # noqa: E402
from models import AlbumInfo, utc_now  # noqa: E402
from services import album_info, album_view, local_albums  # noqa: E402
from services import musicbrainz as mb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_MB_INTERVAL = 1.5
TYPICAL_S, WORST_S = 3, 12
SOURCE_CHOICES = {"musicbrainz": ("musicbrainz",), "deezer": ("deezer",), "any": album_info.SOURCES}


class LockHeld(Exception):
    pass


def guard_test_database(db_path: Path) -> None:
    """Under pytest, refuse any database that is not a temp file outside the repo."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    path = Path(db_path).resolve()
    if not path.is_relative_to(Path(tempfile.gettempdir()).resolve()) or path.is_relative_to(REPO_ROOT):
        print(f"Refusing to run against {path} under pytest.", file=sys.stderr)
        raise SystemExit(2)


def describe(row: AlbumInfo | None, mismatch: bool) -> str:
    if row is not None and row.status == "found":
        return "mismatch" if mismatch else "found"
    if row is None or album_info.is_due(row, mismatch):
        return "due"
    return f"{row.status} (retry {row.retry_at:%Y-%m-%d})"


async def survey(session_factory) -> list[tuple[local_albums.LocalAlbum, str]]:
    """Every local album with its album-info state; reads only."""
    async with session_factory() as session:
        albums = [a for a in await local_albums.scan(session) if a.kind == "album"]
        try:
            rows = {r.album_key: r for r in (await session.scalars(select(AlbumInfo))).all()}
        except OperationalError:
            rows = {}
        states = []
        for album in sorted(albums, key=lambda a: (a.artist.casefold(), a.name.casefold())):
            row = rows.get(album.key)
            loaded = await local_albums.load(session, album.key) if row is not None else None
            states.append((album, describe(row, bool(loaded) and album_info.mismatched(row, loaded[1]))))
    return states


async def dry_run(db_path: Path) -> int:
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{db_path}?mode=ro&uri=true")
    try:
        states = await survey(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
    for album, state in states:
        print(f"{album.artist} — {album.name}: {state}")
    due = sum(state in ("due", "mismatch") for _, state in states)
    print(f"{len(states)} albums: {sum(s == 'found' for _, s in states)} found, {due} due. "
          f"Estimated time with --apply: ~{due * TYPICAL_S // 60} min (worst {due * WORST_S // 60} min).")
    print("Dry run: nothing requested or written; rerun with --apply.")
    return 0


def _lock_payload() -> str:
    now = utc_now().isoformat()
    return json.dumps({"host": socket.gethostname(), "pid": os.getpid(), "started_at": now, "heartbeat": now})


def take_lock(path: Path) -> None:
    """Create the lock, replacing one whose heartbeat (mtime) is older than LOCK_STALE_S."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - path.stat().st_mtime
            if age < album_info.LOCK_STALE_S:
                raise LockHeld(f"Another enrich run holds {path} (heartbeat {age:.0f} s ago).") from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_lock_payload())
        return
    raise LockHeld(f"Could not take {path}.")


def heartbeat(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat"] = utc_now().isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")


async def _result_line(key: str, outcome: str) -> str:
    if outcome != "found":
        return f"-> {outcome.replace('_', ' ')}"
    async with db.async_session() as session:
        row = await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == key))
        view = await album_view.load_group(session, key)
    missing = sum(r.state == "missing" for r in view.view_rows) if view else 0
    return f"-> {row.source} {row.confidence:.2f} ({row.total_tracks} tracks, {missing} missing)"


async def _error_line(key: str) -> str:
    async with db.async_session() as session:
        error = await session.scalar(select(AlbumInfo.last_error).where(AlbumInfo.album_key == key))
    return f"-> error: {error or 'unknown'}"


async def apply(args) -> int:
    await db.init_db()
    async with db.async_session() as session:
        await album_info.rekey_stale_rows(session)
    lock = album_info.lock_path()
    try:
        take_lock(lock)
    except LockHeld as e:
        print(e, file=sys.stderr)
        return 1
    mb.MIN_INTERVAL = SCRIPT_MB_INTERVAL
    try:
        states = await survey(db.async_session)
        todo = [a for a, state in states if args.force or state in ("due", "mismatch")]
        if args.key:
            todo = [a for a in todo if a.key == args.key]
        todo = todo[:args.limit] if args.limit else todo
        errors = 0
        for i, album in enumerate(todo, 1):
            outcome = await album_info.lookup(album.key, sources=SOURCE_CHOICES[args.source])
            errors += outcome == "error"
            line = await (_error_line(album.key) if outcome == "error" else _result_line(album.key, outcome))
            print(f"[{i}/{len(todo)}] {album.artist} — {album.name}  {line}", flush=True)
            heartbeat(lock)
        if args.prune:
            async with db.async_session() as session:
                live = {a.key for a in await local_albums.scan(session)}
                pruned = await album_info.prune(session, live)
            print(f"Pruned album info of {len(pruned)} albums no longer in the library.")
    finally:
        lock.unlink(missing_ok=True)
    return 1 if todo and errors == len(todo) else 0


async def run(args) -> int:
    guard_test_database(db.DB_PATH)
    return await apply(args) if args.apply else await dry_run(db.DB_PATH)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prefetch album info for every album in the library.")
    parser.add_argument("--apply", action="store_true", help="look albums up and store the results")
    parser.add_argument("--limit", type=int, default=0, help="at most N albums")
    parser.add_argument("--force", action="store_true", help="look up albums that already have info too")
    parser.add_argument("--key", help="only this album key")
    parser.add_argument("--source", choices=sorted(SOURCE_CHOICES), default="any")
    parser.add_argument("--prune", action="store_true", help="with --apply: drop info of albums no longer in the library")
    args = parser.parse_args(argv)
    if args.prune and not args.apply:
        parser.error("--prune needs --apply")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
