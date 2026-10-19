import asyncio
import json
import os
import time
from datetime import timedelta

import pytest

from config import settings
from models import AlbumInfo, utc_now
from services import album_info, local_albums
from services.local_albums import album_key
from tests.album_fakes import seed

KEY = album_key("Hot Fuss", "The Killers", "The Killers").key
BRIGHTSIDE = json.dumps([{"disc": 1, "position": 1, "title": "Mr. Brightside", "artist": "The Killers",
                          "duration_ms": None, "isrc": None, "source_track_id": "r"}])


@pytest.fixture
def lookups_on(monkeypatch):
    monkeypatch.setattr(settings, "album_info_lookups", True)


@pytest.fixture
def lookups(monkeypatch):
    """album_info.lookup replaced: records keys and waits for `release` before finishing."""
    calls: list[str] = []
    release = asyncio.Event()

    async def fake_lookup(key, pin=None, **kwargs):
        calls.append(key)
        await release.wait()
        return "found"

    monkeypatch.setattr(album_info, "lookup", fake_lookup)
    return calls, release


@pytest.fixture
def lock_file():
    path = album_info.lock_path()
    yield path
    path.unlink(missing_ok=True)


async def ensure(db, key=KEY, **kwargs):
    async with db() as session:
        return await album_info.ensure(session, key, **kwargs)


async def add_row(db, **fields):
    values = {"album_key": KEY, "key_version": local_albums.KEY_VERSION, "key_album_raw": "Hot Fuss",
              "key_artist_raw": "The Killers", "status": "not_found", "fetched_at": utc_now(), **fields}
    async with db() as session:
        session.add(AlbumInfo(**values))
        await session.commit()


async def test_a_second_open_while_pending_queues_nothing_more(db, lookups_on, lookups):
    calls, release = lookups
    await seed(db)

    assert await ensure(db) == "pending"
    assert await ensure(db) == "pending"
    await asyncio.sleep(0)
    release.set()
    await album_info._queue.join()

    assert calls == [KEY] and album_info.pending_for(KEY) is None


async def test_a_found_album_is_settled(db, lookups_on, lookups):
    await seed(db)
    await add_row(db, status="found", tracks_json=BRIGHTSIDE)

    assert await ensure(db) == "settled"


async def test_a_found_album_that_no_longer_matches_is_looked_up_again(db, lookups_on, lookups):
    await seed(db, title="Somebody Told Me")
    await add_row(db, status="found", tracks_json=BRIGHTSIDE)

    assert await ensure(db) == "pending"


async def test_a_not_found_album_waits_for_its_retry_date(db, lookups_on, lookups):
    await seed(db)
    await add_row(db, retry_at=utc_now() + timedelta(days=3))

    assert await ensure(db) == "settled"
    assert await ensure(db, force=True) == "pending"


async def test_a_due_retry_is_queued(db, lookups_on, lookups):
    await seed(db)
    await add_row(db, retry_at=utc_now() - timedelta(minutes=1))

    assert await ensure(db) == "pending"


async def test_lookups_turned_off_are_disabled(db, lookups):
    assert settings.album_info_lookups is False

    assert await ensure(db) == "disabled"


async def test_after_stop_nothing_is_queued(db, lookups_on, lookups):
    await album_info.stop()

    assert await ensure(db) == "disabled"


async def test_loose_songs_are_never_looked_up(db, lookups_on):
    assert await ensure(db, key="loose") == "never"


async def test_a_running_enrich_script_makes_the_server_busy(db, lookups_on, lookups, lock_file):
    lock_file.write_text("{}", encoding="utf-8")

    assert await ensure(db) == "busy"


async def test_a_stale_script_lock_is_ignored(db, lookups_on, lookups, lock_file):
    lock_file.write_text("{}", encoding="utf-8")
    old = time.time() - album_info.LOCK_STALE_S - 5
    os.utime(lock_file, (old, old))

    assert await ensure(db) == "pending"


async def test_a_full_queue_is_busy(db, lookups_on, lookups, monkeypatch):
    monkeypatch.setattr(album_info, "QUEUE_MAX", 0)

    assert await ensure(db) == "busy"


async def test_a_dead_worker_task_is_replaced(db, lookups_on, lookups):
    calls, release = lookups
    release.set()

    async def dead():
        return None

    album_info._task = asyncio.create_task(dead())
    await album_info._task

    assert await ensure(db) == "pending"
    await album_info._queue.join()
    assert calls == [KEY]


async def test_the_worker_clears_pending_when_a_lookup_raises(db, lookups_on, monkeypatch):
    async def boom(key, pin=None, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(album_info, "lookup", boom)

    assert await ensure(db) == "pending"
    await album_info._queue.join()

    assert album_info.pending_for(KEY) is None
    assert await ensure(db) == "pending"


async def test_status_polls_until_settled_and_stops_after_the_cap(client, db, lookups_on, lookups, monkeypatch):
    calls, release = lookups
    now = [1000.0]
    monkeypatch.setattr(album_info, "_monotonic", lambda: now[0])
    await seed(db)
    assert (await client.post(f"/api/albums/{KEY}/info/ensure")).status_code == 202

    assert (await client.get(f"/api/albums/{KEY}/info/status")).status_code == 204
    now[0] += album_info.STATUS_CAP_S
    capped = await client.get(f"/api/albums/{KEY}/info/status")
    assert capped.status_code == 286 and "Still looking" in capped.text

    release.set()
    await album_info._queue.join()
    settled = await client.get(f"/api/albums/{KEY}/info/status")
    assert settled.status_code == 200
    assert json.loads(settled.headers["HX-Trigger"]) == {"album-info:ready": {"key": KEY}}


async def test_ensure_answers_204_when_nothing_is_due(client, db, lookups_on, lookups):
    await seed(db)
    await add_row(db, status="found", tracks_json=BRIGHTSIDE)

    assert (await client.post(f"/api/albums/{KEY}/info/ensure")).status_code == 204


@pytest.fixture
def pinned_lookups(monkeypatch):
    """album_info.lookup replaced: records (key, pin) and waits for `release` before finishing."""
    calls: list[tuple[str, tuple | None]] = []
    release = asyncio.Event()

    async def fake_lookup(key, pin=None, **kwargs):
        calls.append((key, pin))
        await release.wait()
        return "found"

    monkeypatch.setattr(album_info, "lookup", fake_lookup)
    return calls, release


async def test_a_pin_sent_while_a_lookup_runs_is_looked_up_next(db, lookups_on, pinned_lookups):
    calls, release = pinned_lookups
    await seed(db)
    pin = ("musicbrainz", "11111111-1111-1111-1111-111111111111")

    assert await ensure(db) == "pending"
    await asyncio.sleep(0)
    assert await ensure(db, force=True, pin=pin) == "pending"
    release.set()
    await album_info._queue.join()

    assert calls == [(KEY, None), (KEY, pin)] and album_info.pending_for(KEY) is None


async def test_not_this_album_while_a_lookup_runs_looks_again_afterwards(db, lookups_on, pinned_lookups):
    calls, release = pinned_lookups
    await seed(db)
    await add_row(db, status="found", source="musicbrainz", source_id="m", tracks_json=BRIGHTSIDE)

    assert await ensure(db, force=True) == "pending"
    await asyncio.sleep(0)
    async with db() as session:
        assert await album_info.reject(session, KEY)
    release.set()
    await album_info._queue.join()

    assert calls == [(KEY, None), (KEY, None)]


async def test_queued_lookups_are_skipped_once_the_enrich_script_holds_the_lock(db, lookups_on, lookups, lock_file):
    calls, release = lookups
    release.set()
    await seed(db)

    album_info._pending[KEY] = album_info._monotonic()
    album_info._queue.put_nowait((KEY, None))
    lock_file.write_text("{}", encoding="utf-8")
    album_info._start_worker()
    await album_info._queue.join()

    assert calls == [] and album_info.pending_for(KEY) is None
