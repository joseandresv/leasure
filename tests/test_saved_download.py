"""Saved downloads: one queue function that never duplicates a library or queued song, and the Download all
confirmation numbers (count, size, days, disk floor) behind a selection token."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from config import settings
from models import Playlist, PlaylistTrack, Track
from models_saved import SavedBatch, SavedBatchItem, SavedItem
from services import qobuz_engine, saved_download, yt_engine
from services.saved_listing import ListFilters
from services.saved_store import SavedData
from tests.qobuz_fakes import store_session
from tests.saved_fakes import add_track, seed_collection
from tests.saved_fakes import saved_song as song
from tests.saved_fakes import seed_saved as seed


class Enqueued(list):
    async def __call__(self, track_id, **kwargs):
        self.append(track_id)


@pytest.fixture
def enqueue():
    return Enqueued()


async def picks_for(db, item_id):
    async with db() as session:
        item = await session.get(SavedItem, item_id)
        if item.kind == "track":
            return await saved_download.song_picks(session, item)
        return await saved_download.collection_picks(session, item)


async def queue(db, picks, enqueue, **kwargs):
    async with db() as session:
        return await saved_download.queue_saved(session, picks, "native", enqueue, **kwargs)


async def tracks(db) -> list[Track]:
    async with db() as session:
        return list((await session.scalars(select(Track).order_by(Track.id))).all())


async def test_a_saved_song_queues_once(db, enqueue):
    [item] = await seed(db, "spotify", "track", [song(1)])

    first = await queue(db, await picks_for(db, item.id), enqueue)
    second = await queue(db, await picks_for(db, item.id), enqueue)

    rows = await tracks(db)
    assert len(rows) == 1 and enqueue == [rows[0].id]
    assert first.badge()["status"] == "queued" and second.badge()["status"] == "already_queued"


async def test_a_library_song_is_not_queued(db, enqueue):
    await add_track(db, title="Song 1", artist="Artist", album="Album", spotify_uri="spotify:track:t1")
    [item] = await seed(db, "spotify", "track", [song(1)])

    result = await queue(db, await picks_for(db, item.id), enqueue)

    assert result.badge()["status"] == "already_downloaded" and enqueue == [] and len(await tracks(db)) == 1


async def test_an_errored_row_with_the_same_id_is_requeued_not_duplicated(db, enqueue):
    failed = await add_track(db, title="Song 1", artist="Artist", album="Album", spotify_uri="spotify:track:t1",
                             status="error", error_message="boom")
    [item] = await seed(db, "spotify", "track", [song(1)])

    await queue(db, await picks_for(db, item.id), enqueue)

    rows = await tracks(db)
    assert [(r.id, r.status, r.error_message) for r in rows] == [(failed.id, "pending", None)]
    assert enqueue == [failed.id]


async def test_a_song_on_another_album_is_skipped_unless_asked(db, enqueue):
    await add_track(db, title="Song 1", artist="Artist", album="Greatest Hits", duration_ms=240_000)
    [item] = await seed(db, "spotify", "track", [song(1)])

    skipped = await queue(db, await picks_for(db, item.id), enqueue)
    included = await queue(db, await picks_for(db, item.id), enqueue, include_other_album=True)

    assert skipped.counts == {"other_album": 1} and included.counts == {"queue": 1}


async def test_the_same_song_twice_in_one_request_queues_once(db, enqueue):
    item = await seed_collection(db, "spotify", "playlist", [song(1), song(1)])

    result = await queue(db, await picks_for(db, item.id), enqueue)

    assert result.counts == {"queue": 1, "duplicate": 1} and len(await tracks(db)) == 1


async def test_two_concurrent_requests_create_one_row(db, enqueue):
    [item] = await seed(db, "spotify", "track", [song(1)])
    picks = await picks_for(db, item.id)

    await asyncio.gather(queue(db, picks, enqueue), queue(db, picks, enqueue))

    assert len(await tracks(db)) == 1


async def test_a_qobuz_favorite_downloads_by_its_id(db, enqueue):
    [item] = await seed(db, "qobuz", "track", [SavedData(source_id="q9", title="So What (Live)", artist="Miles Davis",
                                                         isrc="USSM15900113", qobuz_streamable=True)])

    await queue(db, await picks_for(db, item.id), enqueue)

    [row] = await tracks(db)
    assert (row.source, row.qobuz_id, row.qobuz_match_method, row.title) == ("qobuz", "q9", "id", "So What (Live)")


async def test_a_non_streamable_favorite_is_not_queued(db, enqueue):
    [item] = await seed(db, "qobuz", "track", [SavedData(source_id="q9", title="So What", artist="Miles Davis",
                                                         readable=False, qobuz_streamable=False)])

    result = await queue(db, await picks_for(db, item.id), enqueue)

    assert result.counts == {"not_streamable": 1} and await tracks(db) == []


async def test_a_song_saved_on_two_services_keeps_both_ids(db, enqueue):
    await seed(db, "spotify", "track", [song(1)])
    [qobuz] = await seed(db, "qobuz", "track", [SavedData(source_id="q1", title="Song 1", artist="Artist",
                                                          qobuz_streamable=True)])

    await queue(db, await picks_for(db, qobuz.id), enqueue)

    [row] = await tracks(db)
    assert (row.source, row.qobuz_id, row.spotify_uri) == ("qobuz", "q1", "spotify:track:t1")


async def test_a_saved_playlist_becomes_a_playlist_for_the_card(db, enqueue):
    have = await add_track(db, title="Song 2", artist="Artist", album="Album", spotify_uri="spotify:track:t2")
    item = await seed_collection(db, "spotify", "playlist", [song(1), song(2)])

    await queue(db, await picks_for(db, item.id), enqueue)

    async with db() as session:
        playlist = await session.scalar(select(Playlist))
        links = (await session.execute(select(PlaylistTrack.track_id, PlaylistTrack.position)
                                       .order_by(PlaylistTrack.position))).all()
    assert (playlist.source, playlist.source_id, playlist.name) == ("spotify", "c1", "Collection")
    assert [position for _, position in links] == [0, 1] and links[1][0] == have.id


@pytest.fixture
def room(monkeypatch):
    """Frozen counters and disk: 60 bulk Qobuz slots free today, 40 YouTube, 500 GB free."""
    store_session()
    monkeypatch.setattr(qobuz_engine, "downloads_today", lambda: 0)
    monkeypatch.setattr(yt_engine, "downloads_today", lambda: 0)
    disk = SimpleNamespace(free=500 * 1000**3)
    monkeypatch.setattr(saved_download.shutil, "disk_usage", lambda _path: disk)
    return disk


async def plan_for(db, f=ListFilters(), fmt="native", **kwargs):
    async with db() as session:
        selection = await saved_download.selection_for_filters(session, f)
        return await saved_download.make_plan(session, selection, fmt, include_other_album=f.other_album, **kwargs)


async def test_the_confirmation_counts_days_from_the_bulk_share(db, room):
    await seed(db, "spotify", "track", [song(n) for n in range(130)])
    await add_track(db, title="Song 0", artist="Artist", album="Album", spotify_uri="spotify:track:t0")

    plan = await plan_for(db)

    numbers = plan.numbers
    assert (numbers["total"], numbers["in_library"], numbers["per_day"]) == (129, 1, 60)
    assert numbers["days_at_least"] == 3 and numbers["route"] == "qobuz" and plan.refused is None
    assert 3.5 < numbers["size_gb"] < 4.5


async def test_days_count_what_is_left_of_today(db, room):
    qobuz_engine.reserve_daily_slot("bulk")
    await seed(db, "spotify", "track", [song(n) for n in range(60)])

    assert (await plan_for(db)).numbers["days_at_least"] == 2


async def test_mp3_uses_the_youtube_share(db, room):
    await seed(db, "spotify", "track", [song(n) for n in range(100)])

    numbers = (await plan_for(db, fmt="mp3")).numbers

    assert (numbers["route"], numbers["per_day"]) == ("youtube", settings.yt_max_downloads_per_day - 10)
    assert numbers["days_at_least"] == 3 and numbers["size_gb"] == 1.0


async def test_a_batch_that_would_leave_under_20_gb_free_is_refused(db, room):
    room.free = 21 * 1000**3
    await seed(db, "spotify", "track", [song(n) for n in range(100)])

    plan = await plan_for(db)

    assert plan.numbers["space_ok"] is False and "free space" in plan.refused


async def test_a_stale_selection_recounts_and_creates_nothing(db, room):
    await seed(db, "spotify", "track", [song(n) for n in range(3)])
    token = (await plan_for(db)).token
    await seed(db, "spotify", "track", [song(n) for n in range(4)])

    async with db() as session:
        selection = await saved_download.selection_for_filters(session, ListFilters())
        plan, batch = await saved_download.start_batch(session, selection, "native", include_other_album=False,
                                                       token=token)
        batches = await session.scalar(select(func.count(SavedBatch.id)))

    assert batch is None and plan.numbers["total"] == 4 and batches == 0


async def test_download_all_stores_pointers_and_creates_no_track_rows(db, room):
    await seed(db, "spotify", "track", [song(n) for n in range(5)])
    await seed(db, "qobuz", "track", [SavedData(source_id="q9", title="Other", artist="Someone", qobuz_streamable=True)])
    plan = await plan_for(db)

    async with db() as session:
        selection = await saved_download.selection_for_filters(session, ListFilters())
        _, batch = await saved_download.start_batch(session, selection, "native", include_other_album=False,
                                                    token=plan.token)
        items = (await session.scalars(select(SavedBatchItem).order_by(SavedBatchItem.position))).all()

    assert batch.state == "active" and await tracks(db) == []
    assert sorted((i.route, i.quality) for i in items) == [("qobuz", "flac_lossless")] + [("qobuz", "native")] * 5


async def test_unread_albums_are_counted_from_their_track_count(db, room):
    await seed(db, "qobuz", "album", [SavedData(source_id="qa1", title="Album 1", artist="Miles Davis", track_count=9)])

    numbers = (await plan_for(db, ListFilters(kind="album"))).numbers

    assert (numbers["albums_to_read"], numbers["album_songs_estimated"], numbers["total"]) == (1, 9, 9)
