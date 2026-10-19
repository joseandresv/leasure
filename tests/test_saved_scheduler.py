"""When the Saved scheduler refreshes each source, and the sweep rules it relies on."""
from datetime import datetime, timedelta

import pytest

from models import utc_now
from models_saved import SavedItem, SavedSyncState
from services import qobuz_session, saved_store, saved_sync
from services.saved_store import SavedData, sweep, upsert_items
from tests.qobuz_fakes import TOKEN
from tests.saved_fakes import add_track, items, state


@pytest.fixture
def scheduler(db):
    return saved_sync.SavedSync(db)


async def put_state(db, source, kind, **values):
    async with db() as session:
        session.add(SavedSyncState(source=source, kind=kind, **values))
        await session.commit()


def at_local_hour(monkeypatch, hour: int):
    real = datetime

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = real.now().astimezone().replace(hour=hour, minute=5)
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(saved_sync, "datetime", Fixed)


async def test_spotify_is_never_read_automatically_the_first_time(scheduler):
    assert await scheduler.due("spotify") is False


async def test_spotify_refreshes_once_a_day_after_four(db, scheduler, monkeypatch):
    yesterday = utc_now() - timedelta(days=1, hours=1)
    await put_state(db, "spotify", "track", last_full_at=yesterday, last_run_at=yesterday)
    at_local_hour(monkeypatch, 3)
    assert await scheduler.due("spotify") is False
    at_local_hour(monkeypatch, 5)
    assert await scheduler.due("spotify") is True


async def test_qobuz_is_read_once_signed_in_then_every_twelve_hours(db, scheduler):
    assert await scheduler.due("qobuz") is False
    qobuz_session.save(qobuz_session.StoredSession(user_id="42", user_auth_token=TOKEN))
    assert await scheduler.due("qobuz") is True
    await put_state(db, "qobuz", "track", last_run_at=utc_now() - timedelta(hours=2))
    assert await scheduler.due("qobuz") is False


async def test_signing_in_again_reads_qobuz_on_the_next_tick(db, scheduler):
    await put_state(db, "qobuz", "track", state="blocked", blocked_reason="signed_out", last_run_at=utc_now())
    assert await scheduler.due("qobuz") is False
    qobuz_session.save(qobuz_session.StoredSession(user_id="42", user_auth_token=TOKEN))
    assert await scheduler.due("qobuz") is True


async def test_a_paused_source_waits_for_its_resume_time(db, scheduler):
    qobuz_session.save(qobuz_session.StoredSession(user_id="42", user_auth_token=TOKEN))
    await put_state(db, "qobuz", "track", state="paused", resume_after=utc_now() + timedelta(minutes=5),
                    last_run_at=utc_now() - timedelta(days=2))
    assert await scheduler.due("qobuz") is False
    async with db() as session:
        row = await saved_store.sync_state(session, "qobuz", "track")
        row.resume_after = utc_now() - timedelta(minutes=1)
        await session.commit()
    assert await scheduler.due("qobuz") is True


async def test_start_clears_states_a_dead_process_left_running(db, scheduler, monkeypatch):
    await put_state(db, "youtube", "track", state="running", phase_text="1 / 2")
    monkeypatch.setattr(saved_sync, "TICK_S", 3600)
    monkeypatch.setattr(scheduler, "tick", lambda: _noop())
    await scheduler.start()
    await scheduler.stop()
    assert (await state(db, "youtube", "track")).state == "idle"


async def _noop():
    return None


async def test_a_library_change_triggers_one_rematch(db, scheduler, monkeypatch):
    rematches = []

    async def counted(_factory):
        rematches.append(1)

    monkeypatch.setattr(saved_sync.saved_match, "rematch_all", counted)
    monkeypatch.setattr(scheduler, "due", lambda _source: _false())
    await scheduler.tick()
    await scheduler._rematch_task
    await scheduler.tick()
    await add_track(db)
    await scheduler.tick()
    await scheduler._rematch_task
    assert len(rematches) == 2


async def _false():
    return False


async def seed(db, n):
    async with db() as session:
        await upsert_items(session, "spotify", "track", [SavedData(source_id=f"t{i}", title=f"S{i}") for i in range(n)],
                           now=utc_now())
        await session.commit()


async def sweep_ids(db, ids, **kwargs):
    async with db() as session:
        st = await saved_store.sync_state(session, "spotify", "track")
        outcome = await sweep(session, st, ids, now=utc_now(), **kwargs)
        await session.commit()
    return outcome


async def ids_of(db, *source_ids):
    return {r.id for r in await items(db, source="spotify", kind="track") if r.source_id in source_ids}


async def test_a_small_removal_applies_at_once(db):
    await seed(db, 20)
    assert (await sweep_ids(db, await ids_of(db, "t0"))).removed == 1


async def test_more_than_a_tenth_waits_and_only_what_two_passes_agree_on_goes(db):
    await seed(db, 10)
    first = await sweep_ids(db, await ids_of(db, "t0", "t1", "t2"))
    assert (first.removed, first.deferred) == (0, 3)
    second = await sweep_ids(db, await ids_of(db, "t1", "t2", "t3"))
    assert (second.removed, second.deferred) == (2, 1)
    assert {r.source_id for r in await items(db, source="spotify", kind="track") if r.removed_at} == {"t1", "t2"}


async def test_removing_everything_always_waits(db):
    await seed(db, 1)
    assert (await sweep_ids(db, await ids_of(db, "t0"))).deferred == 1


async def test_removed_items_are_purged_after_ninety_days(db):
    await seed(db, 2)
    async with db() as session:
        for row in (await session.scalars(SavedItem.__table__.select())).all():
            item = await session.get(SavedItem, row)
            item.removed_at = utc_now() - timedelta(days=91) if item.source_id == "t0" else None
        await session.commit()
        assert await saved_store.purge_removed(session) == 1
        await session.commit()
    assert [r.source_id for r in await items(db, source="spotify")] == ["t1"]
