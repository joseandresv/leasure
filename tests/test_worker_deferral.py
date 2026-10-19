import asyncio
import sqlite3
import time

from config import settings
from services import downloader, qobuz_client, qobuz_engine
from services.qobuz_engine import QobuzSkip
from services.yt_engine import CapDeferred
from tests.qobuz_fakes import get_track, seed_track, store_session
from worker import DOWNLOAD_PRIORITY, UPGRADE_PRIORITY, DownloadWorker


def defer_for(seconds):
    async def deferred(track_id):
        raise CapDeferred("Daily Qobuz cap reached (100 today) — this FLAC track stays queued.", retry_after_s=seconds)
    return deferred


async def test_a_deferred_track_stays_pending_and_out_of_the_queue(db, monkeypatch):
    monkeypatch.setattr(downloader, "download_track", defer_for(3600))
    track_id = await seed_track(db)
    worker = DownloadWorker()
    started = time.monotonic()

    await worker._process_track(0, track_id)

    track = await get_track(db, track_id)
    assert (track.status, worker.queue_size) == ("pending", 0)
    assert "stays queued" in track.error_message and track_id in worker._deferred
    assert time.monotonic() - started < 1


async def test_a_scheduler_tick_requeues_a_track_once_it_is_due(db, monkeypatch):
    monkeypatch.setattr(downloader, "download_track", defer_for(0))
    track_id = await seed_track(db)
    worker = DownloadWorker()
    await worker._process_track(0, track_id)

    await worker._tick()

    assert worker.queue.get_nowait()[2] == track_id and worker._deferred == {}


async def test_downloads_go_ahead_of_upgrades():
    worker = DownloadWorker()
    await worker.enqueue(1, priority=UPGRADE_PRIORITY)
    await worker.enqueue(2)

    assert [worker.queue.get_nowait()[0] for _ in range(2)] == [DOWNLOAD_PRIORITY, UPGRADE_PRIORITY]


async def test_upgrades_are_fed_only_up_to_the_cap_and_never_ahead_of_a_download(db, monkeypatch):
    store_session()
    monkeypatch.setattr(settings, "qobuz_max_downloads_per_day", 2)
    monkeypatch.setitem(qobuz_engine.DAILY_SHARES, "upgrade", 1.0)
    ids = [await seed_track(db, status="done", file_path=f"/l/{i}.m4a", upgrade_state="queued") for i in range(3)]
    worker = DownloadWorker()
    await worker.enqueue(99)

    await worker.top_up_upgrades()
    assert worker.queue_size == 1

    worker.queue.get_nowait()
    worker._queued.clear()
    await worker.top_up_upgrades()
    assert sorted(worker.queue.get_nowait()[2] for _ in range(worker.queue_size)) == ids[:2]


async def test_an_unexpected_upgrade_crash_stores_only_the_class_name(db, monkeypatch):
    async def crash(track_id):
        raise RuntimeError(f"boom with secret {qobuz_client.API_BASE}?user_auth_token=abc")

    monkeypatch.setattr(qobuz_engine, "qobuz_upgrade", crash)
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="queued")

    await DownloadWorker()._process_track(0, track_id)

    track = await get_track(db, track_id)
    assert (track.status, track.upgrade_state, track.upgrade_error) == ("done", "error", "Upgrade failed (RuntimeError).")


def skip_upgrade(monkeypatch, reason, message):
    async def skip(track_id):
        raise QobuzSkip(reason, message)

    monkeypatch.setattr(qobuz_engine, "qobuz_upgrade", skip)


async def test_an_unmatched_upgrade_is_dismissed_and_left_out_of_upgrade_all(client, db, monkeypatch):
    store_session()
    skip_upgrade(monkeypatch, "no_match", qobuz_engine.UPGRADE_NO_MATCH)
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="queued")

    await DownloadWorker()._process_track(0, track_id)
    track = await get_track(db, track_id)
    resp = await client.post("/api/qobuz/upgrade/all")

    assert (track.upgrade_state, track.upgrade_error, track.qobuz_skip_reason) == (None, None, "no_match")
    assert resp.json()["skipped"] == {str(track_id): "unmatched"}


async def test_an_upgrade_skipped_for_another_reason_stays_an_error(db, monkeypatch):
    skip_upgrade(monkeypatch, "not_streamable", "Qobuz cannot stream this track.")
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="queued")

    await DownloadWorker()._process_track(0, track_id)

    track = await get_track(db, track_id)
    assert (track.upgrade_state, track.qobuz_skip_reason) == ("error", "not_streamable")


async def test_the_worker_loop_stores_a_redacted_message(db, monkeypatch):
    secret = "tok_" + "S3cr3tS3cr3t" * 3
    qobuz_client.register_secret(secret)

    async def leak(track_id):
        raise RuntimeError(f"request failed with {secret}")

    monkeypatch.setattr(downloader, "download_track", leak)
    track_id = await seed_track(db)
    worker = DownloadWorker()
    await worker.enqueue(track_id)
    task = asyncio.create_task(worker._worker(0))
    await worker.queue.join()
    task.cancel()

    message = (await get_track(db, track_id)).error_message
    assert secret not in message and "request failed" in message


async def test_recovery_puts_an_interrupted_upgrade_back_in_line(db):
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="downloading")

    await DownloadWorker()._recover_orphaned()

    assert (await get_track(db, track_id)).upgrade_state == "queued"


async def test_an_upgrade_whose_error_cannot_be_recorded_leaves_the_track_done(db, monkeypatch):
    async def crash(track_id):
        raise RuntimeError("boom")

    set_upgrade = DownloadWorker._set_upgrade

    async def locked_on_error(self, track_id, state, error, **kwargs):
        if state == "error":
            raise sqlite3.OperationalError("database is locked")
        await set_upgrade(self, track_id, state, error, **kwargs)

    monkeypatch.setattr(qobuz_engine, "qobuz_upgrade", crash)
    monkeypatch.setattr(DownloadWorker, "_set_upgrade", locked_on_error)
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="queued")
    worker = DownloadWorker()
    await worker.enqueue(track_id, priority=UPGRADE_PRIORITY)
    task = asyncio.create_task(worker._worker(0))
    await worker.queue.join()
    task.cancel()

    track = await get_track(db, track_id)
    assert (track.status, track.error_message) == ("done", None)
