"""One asyncio lock per track id, held by anything that moves a track's library file
and commits the new path (album refresh, Qobuz download, FLAC upgrade, revert)."""

import asyncio

_LOCKS: dict[int, asyncio.Lock] = {}


def hold(track_id: int) -> asyncio.Lock:
    lock = _LOCKS.get(track_id)
    if lock is None:
        lock = _LOCKS[track_id] = asyncio.Lock()
    return lock
