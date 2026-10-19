"""The Download all lane: one song in flight, paced, within its share of the caps, with Track rows created only
when a song is handed over; and the downloader's bulk rules (wait for Qobuz, else MP3 320, never AAC)."""
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from models import PlaylistTrack, Track
from models_saved import SavedBatch, SavedBatchItem
from services import downloader, qobuz_engine, qobuz_session, saved_bulk, saved_download, yt_engine
from services.qobuz_engine import QobuzSkip
from services.saved_listing import ListFilters
from services.saved_store import SavedData
from services.yt_engine import CapDeferred, DownloadFailed
from tests.qobuz_fakes import get_track, seed_track, store_session
from tests.saved_fakes import FakeQobuzLibrary, add_track, qalbum, seed_collection
from tests.saved_fakes import saved_song as song
from tests.saved_fakes import seed_saved as seed
from worker import BULK_PRIORITY, DOWNLOAD_PRIORITY, UPGRADE_PRIORITY, DownloadWorker


class Enqueued(list):
    async def __call__(self, track_id, *, priority=DOWNLOAD_PRIORITY):
        self.append((track_id, priority))


@pytest.fixture
def lane(monkeypatch):
    """Qobuz signed in, nothing used today, 500 GB free; returns the enqueue recorder."""
    store_session()
    monkeypatch.setattr(yt_engine, "downloads_today", lambda: 0)
    monkeypatch.setattr(saved_download.shutil, "disk_usage", lambda _path: SimpleNamespace(free=500 * 1000**3))
    return Enqueued()


async def make_batch(db, f=ListFilters(), fmt="native") -> int:
    async with db() as session:
        selection = await saved_download.selection_for_filters(session, f)
        plan = await saved_download.make_plan(session, selection, fmt, include_other_album=False)
        _, batch = await saved_download.start_batch(session, selection, fmt, include_other_album=False, token=plan.token)
        return batch.id


async def batch_items(db, batch_id=None) -> list[SavedBatchItem]:
    async with db() as session:
        stmt = select(SavedBatchItem).order_by(SavedBatchItem.batch_id, SavedBatchItem.position, SavedBatchItem.id)
        if batch_id:
            stmt = stmt.where(SavedBatchItem.batch_id == batch_id)
        return list((await session.scalars(stmt)).all())


async def finish(db, track_id, status="done", *, skip_gap=False):
    async with db() as session:
        track = await session.get(Track, track_id)
        track.status = status
        await session.commit()
    await saved_bulk.reconcile()
    if skip_gap:
        saved_bulk._lane["next_at"] = 0.0


async def track_count(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count(Track.id)))


async def test_one_song_at_a_time_and_its_row_only_when_handed_over(db, lane):
    await seed(db, "spotify", "track", [song(n) for n in range(3)])
    await make_batch(db)
    assert await track_count(db) == 0

    assert await saved_bulk.feed(lane) == "fed"
    assert await saved_bulk.feed(lane) == "busy"

    assert await track_count(db) == 1 and [p for _, p in lane] == [BULK_PRIORITY]
    assert [i.state for i in await batch_items(db)].count("fed") == 1


async def test_a_finished_song_starts_a_two_to_five_minute_gap(db, lane):
    await seed(db, "spotify", "track", [song(n) for n in range(2)])
    await make_batch(db)
    await saved_bulk.feed(lane)
    before = time.time()
    await finish(db, lane[0][0])

    assert await saved_bulk.feed(lane) == "gap"

    assert 120 <= saved_bulk._lane["next_at"] - before <= 301
    assert (await batch_items(db))[0].state == "done"


async def test_at_most_twenty_songs_start_an_hour(db, lane, monkeypatch):
    monkeypatch.setattr(saved_bulk, "BULK_MAX_PER_HOUR", 1)
    await seed(db, "spotify", "track", [song(n) for n in range(2)])
    await make_batch(db)
    await saved_bulk.feed(lane)
    await finish(db, lane[0][0], skip_gap=True)

    assert await saved_bulk.feed(lane) == "hourly"


async def test_bulk_hours_are_respected(db, lane, monkeypatch):
    hour = datetime.now().hour
    monkeypatch.setattr(saved_bulk, "BULK_HOURS", f"{(hour + 1) % 24}-{(hour + 2) % 24}")
    await seed(db, "spotify", "track", [song(1)])
    await make_batch(db)

    assert await saved_bulk.feed(lane) == "hours"


async def test_the_bulk_share_stops_the_lane_while_clicks_still_get_slots(db, lane, monkeypatch):
    monkeypatch.setitem(qobuz_engine.DAILY_SHARES, "bulk", 0.01)
    qobuz_engine.reserve_daily_slot("bulk")
    await seed(db, "spotify", "track", [song(1)])
    await make_batch(db)

    assert await saved_bulk.feed(lane) == "blocked" and saved_bulk._lane["blocked"] == "share"
    assert qobuz_engine.reserve_daily_slot("interactive") is True


async def test_an_expired_qobuz_sign_in_holds_qobuz_songs_but_not_youtube_ones(db, lane):
    store_session(state="expired")
    await seed(db, "spotify", "track", [song(1)])
    await make_batch(db)
    await seed(db, "youtube", "track", [SavedData(source_id="v1", title="Clip", artist="Band")])
    mp3 = await make_batch(db, ListFilters(source="youtube"), fmt="mp3")

    assert await saved_bulk.feed(lane) == "fed"

    fed = [i for i in await batch_items(db) if i.state == "fed"]
    assert [i.batch_id for i in fed] == [mp3]


async def test_best_without_a_qobuz_account_comes_as_mp3(db, lane):
    qobuz_session.clear()
    await seed(db, "spotify", "track", [song(1)])
    await make_batch(db)

    await saved_bulk.feed(lane)

    assert (await get_track(db, lane[0][0])).quality == "mp3_320"


async def test_a_song_queued_by_hand_meanwhile_is_skipped_without_a_new_row(db, lane):
    await seed(db, "spotify", "track", [song(1), song(2)])
    await make_batch(db)
    await add_track(db, title="Song 1", artist="Artist", album="Album", spotify_uri="spotify:track:t1", status="pending")
    await add_track(db, title="Song 2", artist="Artist", album="Album", spotify_uri="spotify:track:t2", status="pending")

    assert await saved_bulk.feed(lane) == "idle"

    assert {i.state for i in await batch_items(db)} == {"skipped_queued"} and await track_count(db) == 2


async def test_a_click_on_a_song_waiting_in_a_batch_queues_it_and_the_batch_skips_it(db, lane):
    [item] = await seed(db, "spotify", "track", [song(1)])
    await make_batch(db)
    clicked = Enqueued()
    async with db() as session:
        picks = await saved_download.song_picks(session, item)
        result = await saved_download.queue_saved(session, picks, "native", clicked)

    assert result.counts == {"queue": 1} and await saved_bulk.feed(lane) == "idle"
    assert [i.state for i in await batch_items(db)] == ["skipped_queued"] and await track_count(db) == 1


async def test_batches_take_turns(db, lane):
    await seed(db, "spotify", "track", [song(1), song(2)])
    first = await make_batch(db, ListFilters(q="song 1"))
    second = await make_batch(db, ListFilters(q="song 2"))

    await saved_bulk.feed(lane)
    await finish(db, lane[0][0], skip_gap=True)
    await saved_bulk.feed(lane)

    fed = [i.batch_id for i in await batch_items(db) if i.track_id]
    assert fed == [first, second]


async def test_cancel_deletes_no_track_and_no_playlist_entry(db, lane):
    await seed_collection(db, "spotify", "playlist", [song(1), song(2), song(3)])
    await add_track(db, title="Song 3", artist="Artist", album="Album", spotify_uri="spotify:track:t3")
    batch_id = await make_batch(db, ListFilters(kind="playlist"))
    await saved_bulk.feed(lane)
    links_before = await _links(db)

    async with db() as session:
        await saved_bulk.cancel(session, batch_id)

    states = sorted(i.state for i in await batch_items(db, batch_id))
    assert states == ["cancelled", "fed"] and await track_count(db) == 2 and await _links(db) == links_before == 2


async def _links(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count(PlaylistTrack.id)))


async def test_albums_are_read_by_the_feeder_not_when_the_batch_is_created(db, lane, monkeypatch):
    fake = FakeQobuzLibrary(albums=[qalbum(1)])
    monkeypatch.setattr(qobuz_session, "make_client", lambda _stored=None: fake)
    await seed(db, "qobuz", "album", [SavedData(source_id="qa1", title="Album 1", artist="Miles Davis", track_count=1)])
    await make_batch(db, ListFilters(kind="album"))
    assert fake.calls == []

    assert await saved_bulk.feed(lane) == "fed"

    assert fake.calls == ["get_album:qa1"]
    states = sorted(i.state for i in await batch_items(db))
    assert states == ["expanded", "fed"]


async def _bulk_row(db, **fields) -> int:
    track_id = await seed_track(db, **fields)
    async with db() as session:
        batch = SavedBatch(state="active", format="native")
        session.add(batch)
        await session.flush()
        session.add(SavedBatchItem(batch_id=batch.id, state="fed", track_id=track_id, data="{}"))
        await session.commit()
    return track_id


@pytest.fixture
def engines(monkeypatch):
    calls = {"qobuz": [], "youtube": [], "outcome": None}

    async def fake_qobuz(track_id, kind="interactive"):
        calls["qobuz"].append(kind)
        raise calls["outcome"]

    async def fake_youtube(track_id):
        calls["youtube"].append(track_id)
        return Path("/library/y.mp3")

    monkeypatch.setattr(qobuz_engine, "qobuz_download", fake_qobuz)
    monkeypatch.setattr(yt_engine, "download", fake_youtube)
    monkeypatch.setattr(yt_engine, "downloads_today", lambda: 0)
    return calls


@pytest.mark.parametrize("reason", ["cap", "unavailable", "cdn_unavailable", "signed_out"])
async def test_a_bulk_best_row_waits_for_qobuz_instead_of_falling_back(db, engines, reason):
    engines["outcome"] = QobuzSkip(reason)
    track_id = await _bulk_row(db, quality="native")

    with pytest.raises(CapDeferred):
        await downloader.download_track(track_id)

    assert engines["youtube"] == [] and engines["qobuz"] == ["bulk"]


async def test_a_bulk_best_row_qobuz_lacks_comes_as_mp3_never_aac(db, engines):
    engines["outcome"] = QobuzSkip("no_match")
    track_id = await _bulk_row(db, quality="native")

    await downloader.download_track(track_id)

    track = await get_track(db, track_id)
    assert engines["youtube"] == [track_id] and (track.quality, track.format) == ("mp3_320", "mp3")


async def test_a_click_still_falls_back_to_the_native_stream(db, engines):
    engines["outcome"] = QobuzSkip("no_match")
    track_id = await seed_track(db, quality="native")

    await downloader.download_track(track_id)

    assert (await get_track(db, track_id)).quality == "native" and engines["qobuz"] == ["interactive"]


async def test_a_flac_only_bulk_row_never_falls_back(db, engines):
    engines["outcome"] = QobuzSkip("no_match")
    track_id = await _bulk_row(db, quality="flac_lossless", source="qobuz", qobuz_id="q9", youtube_id=None)

    with pytest.raises(DownloadFailed):
        await downloader.download_track(track_id)

    assert engines["youtube"] == []


async def test_bulk_leaves_youtube_downloads_for_clicks(db, engines, monkeypatch):
    monkeypatch.setattr(yt_engine, "downloads_today", lambda: yt_engine.MAX_DOWNLOADS_PER_DAY - downloader.BULK_YT_RESERVE)
    track_id = await _bulk_row(db, quality="mp3_320")

    with pytest.raises(CapDeferred, match="Download all"):
        await downloader.download_track(track_id)


async def test_a_recovered_bulk_row_keeps_the_bulk_rules(db, engines):
    engines["outcome"] = QobuzSkip("cap")
    track_id = await _bulk_row(db, quality="native", status="downloading")
    worker = DownloadWorker()

    await worker._recover_orphaned()
    priority, _, queued = worker.queue.get_nowait()
    with pytest.raises(CapDeferred):
        await downloader.download_track(queued)

    assert (queued, priority) == (track_id, DOWNLOAD_PRIORITY)
    assert engines["qobuz"] == ["bulk"] and engines["youtube"] == []


async def test_a_deferred_bulk_row_comes_back_at_bulk_priority(db, monkeypatch):
    async def defer(track_id):
        raise CapDeferred("wait", retry_after_s=0)

    monkeypatch.setattr(downloader, "download_track", defer)
    track_id = await seed_track(db)
    worker = DownloadWorker()
    monkeypatch.setattr(worker, "top_up_bulk", _nothing)

    await worker._process_track(0, track_id, BULK_PRIORITY)
    await worker._tick()

    assert worker.queue.get_nowait()[:1] == (BULK_PRIORITY,)


async def _nothing():
    return None


async def test_upgrades_are_not_held_back_by_bulk_and_stop_at_their_share(db, monkeypatch):
    store_session()
    monkeypatch.setitem(qobuz_engine.DAILY_SHARES, "upgrade", 0.02)
    for i in range(3):
        await seed_track(db, status="done", file_path=f"/l/{i}.m4a", upgrade_state="queued", youtube_id=f"y{i}")
    worker = DownloadWorker()
    await worker.enqueue(99, priority=BULK_PRIORITY)

    await worker.top_up_upgrades()

    priorities = sorted(worker.queue.get_nowait()[0] for _ in range(worker.queue_size))
    assert priorities == [BULK_PRIORITY, UPGRADE_PRIORITY, UPGRADE_PRIORITY]
