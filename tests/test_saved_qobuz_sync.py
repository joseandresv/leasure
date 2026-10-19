"""The Qobuz refresh job of the Saved tab against a scripted library client (no network)."""
import json

import pytest

from config import settings
from models_saved import SavedSyncState
from services import qobuz_client, qobuz_engine, qobuz_session, saved_qobuz
from tests.qobuz_fakes import TOKEN, qtrack
from tests.saved_fakes import FakeQobuzLibrary, entries, items, qalbum, qartist, qplaylist, state


@pytest.fixture
def signed_in():
    qobuz_session.save(qobuz_session.StoredSession(user_id="42", user_auth_token=TOKEN))


def favs(n: int) -> list:
    return [qtrack(id=f"q{i}", title=f"Song {i}", favorited_at=1_760_000_000 - i) for i in range(1, n + 1)]


def no_gap() -> qobuz_client.Throttle:
    return qobuz_client.Throttle(1e9, sleep=lambda _s: None)


async def read(db, fake, *, full=False):
    await saved_qobuz.run(db, full=full, client=fake, gap=no_gap())


async def test_first_read_stores_every_kind_newest_first_with_full_titles(db, signed_in):
    fake = FakeQobuzLibrary(tracks=[qtrack(id="q1", title="Song", version="Live", favorited_at=2),
                                    qtrack(id="q2", title="Other", favorited_at=1)],
                            albums=[qalbum(1)], artists=[qartist(1)], playlists=[qplaylist("p1")],
                            playlist_tracks={"p1": [qtrack(id="q9")]})
    await read(db, fake)
    tracks = await items(db, source="qobuz", kind="track")
    assert [(t.source_id, t.title) for t in tracks] == [("q1", "Song (Live)"), ("q2", "Other")]
    assert tracks[0].qobuz_max_bits == 24 and tracks[0].added_at is not None
    assert len(await items(db, source="qobuz", kind="album")) == 1
    assert len(await items(db, source="qobuz", kind="artist")) == 1
    (playlist,) = await items(db, source="qobuz", kind="playlist")
    assert [e.source_id for e in await entries(db, playlist.id)] == ["q9"] and fake.closed


async def test_a_few_new_favorites_with_trusted_ids_are_fetched_one_by_one_without_paging(db, signed_in):
    fake = FakeQobuzLibrary(tracks=favs(3))
    await read(db, fake)
    fake.tracks = [qtrack(id="q7", title="New", favorited_at=None), *favs(3)]
    fake.calls.clear()
    await read(db, fake)
    assert "get_track:q7" in fake.calls and "iter:tracks" not in fake.calls
    assert [t.source_id for t in await items(db, source="qobuz", kind="track")] == ["q7", "q1", "q2", "q3"]


async def test_trusted_ids_remove_an_unfavorited_track(db, signed_in):
    fake = FakeQobuzLibrary(tracks=favs(20))
    await read(db, fake)
    fake.tracks = favs(20)[1:]
    await read(db, fake)
    removed = [t.source_id for t in await items(db, source="qobuz", kind="track") if t.removed_at]
    assert removed == ["q1"]


async def test_ids_shorter_than_the_total_are_not_trusted_for_removals(db, signed_in):
    fake = FakeQobuzLibrary(tracks=favs(20))
    await read(db, fake)
    fake.ids = {"tracks": ["q2"], "albums": [], "artists": []}
    fake.calls.clear()
    await read(db, fake)
    assert "iter:tracks" in fake.calls
    assert not [t for t in await items(db, source="qobuz", kind="track") if t.removed_at]


async def test_a_mass_disappearance_waits_for_a_second_agreeing_pass(db, signed_in):
    fake = FakeQobuzLibrary(tracks=favs(10))
    await read(db, fake)
    fake.tracks = favs(10)[5:]
    await read(db, fake)
    assert not [t for t in await items(db, source="qobuz", kind="track") if t.removed_at]
    await read(db, fake)
    assert len([t for t in await items(db, source="qobuz", kind="track") if t.removed_at]) == 5


async def test_a_403_on_a_list_endpoint_opens_the_breaker_for_all_qobuz_work(db, signed_in):
    fake = FakeQobuzLibrary(errors={"favorites:tracks:1": qobuz_client.QobuzForbiddenError(status=403)})
    await read(db, fake)
    assert qobuz_engine.breaker_state()["reason"] == "unavailable"
    assert (await state(db, "qobuz", "track")).blocked_reason == "forbidden"
    assert await state(db, "qobuz", "album") is None


async def test_an_expired_sign_in_is_marked_and_blocks_the_source(db, signed_in):
    fake = FakeQobuzLibrary(errors={"favorites:tracks:1": qobuz_client.QobuzAuthError(status=401)})
    await read(db, fake)
    assert qobuz_session.load().state == "expired"
    assert (await state(db, "qobuz", "track")).blocked_reason == "signed_out"


async def test_a_429_sets_the_shared_cooldown(db, signed_in):
    fake = FakeQobuzLibrary(errors={"favorites:tracks:1": qobuz_client.QobuzRateLimitError(retry_after=120)})
    await read(db, fake)
    assert qobuz_engine.cooldown_left() > 100
    assert (await state(db, "qobuz", "track")).state == "paused"
    fake.calls.clear()
    await read(db, fake)
    assert fake.calls == []


async def test_a_kind_the_favorites_check_failed_is_skipped(db, signed_in):
    (settings.data_dir / saved_qobuz.FAVORITES_CHECK_NAME).write_text(json.dumps({"albums_ok": False}))
    try:
        fake = FakeQobuzLibrary(albums=[qalbum(1)])
        await read(db, fake)
    finally:
        (settings.data_dir / saved_qobuz.FAVORITES_CHECK_NAME).unlink()
    assert not [c for c in fake.calls if "albums" in c]
    assert (await state(db, "qobuz", "album")).state == "off"


async def test_an_unexpected_shape_switches_only_that_kind_off(db, signed_in):
    fake = FakeQobuzLibrary(tracks=favs(1), albums=[qalbum(1)],
                            errors={"favorites:albums:1": qobuz_client.QobuzShapeError()})
    await read(db, fake)
    assert (await state(db, "qobuz", "album")).state == "off"
    assert (await state(db, "qobuz", "artist")).state == "idle"


async def test_a_refused_playlist_is_only_marked_unreadable(db, signed_in):
    fake = FakeQobuzLibrary(playlists=[qplaylist("p1"), qplaylist("p2")], playlist_tracks={"p2": [qtrack()]},
                            errors={"playlist:p1": qobuz_client.QobuzNotFoundError(status=404)})
    await read(db, fake)
    readable = {p.source_id: p.readable for p in await items(db, source="qobuz", kind="playlist")}
    assert readable == {"p1": False, "p2": True}


async def test_a_rotated_token_is_stored_after_the_run(db, signed_in):
    fake = FakeQobuzLibrary(rotated_token="tok_" + "Z9y8X7w6V5u4T3s2" * 2)
    await read(db, fake)
    assert qobuz_session.load().user_auth_token == fake.rotated_token


async def test_without_a_sign_in_nothing_is_called(db):
    await saved_qobuz.run(db, client=FakeQobuzLibrary(), gap=no_gap())
    async with db() as session:
        rows = (await session.execute(SavedSyncState.__table__.select())).all()
    assert {row.blocked_reason for row in rows} == {"signed_out"}


async def test_the_sync_gap_spaces_every_call(db, signed_in):
    waits: list[float] = []
    clock = iter(range(0, 10_000, 1))
    gap = qobuz_client.Throttle(60 / saved_qobuz.SYNC_GAP_S, clock=lambda: next(clock), sleep=waits.append)
    await saved_qobuz.run(db, client=FakeQobuzLibrary(tracks=favs(2)), gap=gap)
    assert waits and all(w >= saved_qobuz.SYNC_GAP_S - 1 for w in waits)
