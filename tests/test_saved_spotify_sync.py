"""The Spotify refresh job of the Saved tab against a scripted spotipy client (no network)."""
import json
from datetime import date

import pytest
import spotipy
from sqlalchemy import select

from models import utc_now
from models_saved import SavedItem, SavedSyncState
from services import saved_spotify
from services import spotify_client as sp
from tests.saved_fakes import FakeSpotify, items, liked, paged, rate_limit_error, sp_page, sp_playlist, sp_track, state


@pytest.fixture(autouse=True)
def spotify_files(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_rate_limited_until", 0.0)
    monkeypatch.setattr(sp, "RATE_LIMIT_PATH", tmp_path / "spotify_rate_limit.json")
    monkeypatch.setattr(sp, "CALLS_PATH", tmp_path / "spotify_calls.json")


def pacer():
    return sp.SyncPacer(sleep=lambda _s: None)


async def read(db, fake, *, full=False):
    await saved_spotify.run(db, full=full, client=fake, pacer=pacer())


async def mark_read(db, **values):
    async with db() as session:
        for kind in ("track", "album", "playlist"):
            row = SavedSyncState(source="spotify", kind=kind, state="idle", last_full_at=utc_now(), last_ok_at=utc_now(),
                                 last_run_at=utc_now(), **values)
            session.add(row)
        await session.commit()


async def test_first_read_stores_both_item_shapes_counts_placeholders_and_keeps_local_files_unreadable(db):
    raw = [liked(1, shape="item"), liked(2, shape="track"), {"added_at": "2026-09-01T00:00:00Z", "track": None},
           {"added_at": "2026-08-01T00:00:00Z", "is_local": True,
            "track": {"id": None, "uri": "spotify:local:a:b:Demo:200", "name": "Demo", "album": None}}]
    fake = FakeSpotify(tracks=paged(raw))
    await read(db, fake)
    rows = await items(db, source="spotify", kind="track")
    assert [(r.source_id, r.readable) for r in rows] == [("t01", True), ("t02", True), ("spotify:local:a:b:Demo:200", False)]
    tracks = await state(db, "spotify", "track")
    assert tracks.placeholders == 1 and tracks.last_full_at is not None and tracks.full_requested is False


async def test_the_first_read_leaves_followed_artists_out(db):
    fake = FakeSpotify(tracks=paged([liked(1)]))
    await read(db, fake)
    assert not [c for c in fake.calls if c[0] == "follow"]


async def test_incremental_stops_at_the_first_song_already_stored_with_the_same_added_at(db):
    songs = [liked(n) for n in range(1, 4)]
    fake = FakeSpotify(tracks=paged(songs))
    await read(db, fake)
    fake.tracks = paged([liked(9, day=29)] + songs * 30)
    fake.calls.clear()
    await read(db, fake)
    rows = await items(db, source="spotify", kind="track")
    assert [r.source_id for r in rows] == ["t09", "t01", "t02", "t03"]
    assert [c for c in fake.calls if c[0] == "tracks"] == [("tracks", 0, 50)]


async def test_a_count_mismatch_schedules_one_full_read_and_not_another_within_seven_days(db):
    fake = FakeSpotify(tracks=paged([liked(1), liked(2)]))
    await read(db, fake)
    fake.tracks = paged([liked(1), liked(2)], total=5)
    await read(db, fake)
    first = await state(db, "spotify", "track")
    assert first.full_requested is True and first.last_mismatch_full_at is not None
    async with db() as session:
        row = await session.scalar(select(SavedSyncState).where(SavedSyncState.source == "spotify",
                                                                SavedSyncState.kind == "track"))
        row.full_requested = False
        await session.commit()
    await read(db, fake)
    assert (await state(db, "spotify", "track")).full_requested is False


async def test_paging_ends_on_a_page_that_repeats_forever(db, monkeypatch):
    monkeypatch.setitem(saved_spotify._PAGE_CAP, "track", 5)
    same = sp_page([liked(1)], total=10_000, offset=0)

    class Endless(FakeSpotify):
        def current_user_saved_tracks(self, limit=20, offset=0, market=None):
            self.calls.append(("tracks", offset, limit))
            return same

    fake = Endless()
    await read(db, fake)
    assert len([c for c in fake.calls if c[0] == "tracks"]) <= 5


async def test_a_429_on_page_two_saves_the_cursor_pauses_and_the_next_run_resumes_there(db):
    songs = [liked(n) for n in range(1, 61)]
    pages = paged(songs)
    pages[50] = rate_limit_error()
    fake = FakeSpotify(tracks=pages)
    await read(db, fake)
    paused = await state(db, "spotify", "track")
    assert paused.state == "paused" and paused.blocked_reason == "rate_limited"
    assert json.loads(paused.cursor) == {"offset": 50} and paused.resume_after is not None
    calls = len(fake.calls)
    await read(db, fake)
    assert len(fake.calls) == calls

    sp._rate_limited_until = 0.0
    fake.tracks = paged(songs)
    fake.calls.clear()
    await read(db, fake)
    assert ("tracks", 50, 50) in fake.calls and ("tracks", 0, 50) not in fake.calls
    assert len(await items(db, source="spotify", kind="track")) == 60


async def test_reading_stops_for_the_day_at_the_sync_share_but_counts_interactive_calls(db):
    sp.CALLS_PATH.write_text(json.dumps({"date": saved_date(), "total": 10, "sync": saved_spotify.DAILY_SYNC}))
    fake = FakeSpotify(tracks=paged([liked(1)]))
    await read(db, fake)
    assert fake.calls == []
    assert (await state(db, "spotify", "track")).blocked_reason == "daily_budget"
    sp._count_call()
    assert sp.calls_today() == {"total": 11, "sync": saved_spotify.DAILY_SYNC}


async def test_reading_stops_at_the_app_wide_daily_total(db):
    sp.CALLS_PATH.write_text(json.dumps({"date": saved_date(), "total": saved_spotify.DAILY_TOTAL, "sync": 0}))
    fake = FakeSpotify(tracks=paged([liked(1)]))
    await read(db, fake)
    assert fake.calls == [] and (await state(db, "spotify", "track")).state == "paused"


def test_the_call_counter_restarts_on_a_new_date():
    sp.CALLS_PATH.write_text(json.dumps({"date": "2000-01-01", "total": 299, "sync": 149}))
    with sp.counting_as_sync():
        sp._count_call()
    assert sp.calls_today() == {"total": 1, "sync": 1}


def saved_date() -> str:
    return date.today().isoformat()


async def test_a_full_pass_that_does_not_add_up_removes_nothing(db):
    fake = FakeSpotify(tracks=paged([liked(1), liked(2), liked(3)]))
    await read(db, fake)
    fake.tracks = paged([liked(1)], total=3)
    await read(db, fake, full=True)
    assert all(r.removed_at is None for r in await items(db, source="spotify", kind="track"))


async def test_candidates_spotify_still_reports_saved_are_kept(db):
    songs = [liked(n) for n in range(1, 21)]
    fake = FakeSpotify(tracks=paged(songs), contains={"t02"})
    await read(db, fake)
    fake.tracks = paged([s for s in songs if s["track"]["id"] not in ("t01", "t02")])
    await read(db, fake, full=True)
    removed = {r.source_id for r in await items(db, source="spotify", kind="track") if r.removed_at}
    assert removed == {"t01"}


async def test_an_unavailable_contains_endpoint_leaves_the_decision_to_the_completeness_rules(db):
    songs = [liked(n) for n in range(1, 21)]
    fake = FakeSpotify(tracks=paged(songs), contains=spotipy.SpotifyException(403, -1, "Forbidden"))
    await read(db, fake)
    fake.tracks = paged(songs[1:])
    await read(db, fake, full=True)
    assert {r.source_id for r in await items(db, source="spotify", kind="track") if r.removed_at} == {"t01"}


async def test_unchanged_playlist_snapshots_make_no_item_calls_and_no_fields_filter_is_sent(db):
    fake = FakeSpotify(playlists=paged([sp_playlist("p1")]),
                       playlist_items={"p1": sp_page([liked(5, shape="item")], 1, 0, 100)})
    await read(db, fake)
    assert ("playlist_items", "p1", None) in fake.calls
    (playlist,) = await items(db, source="spotify", kind="playlist")
    assert playlist.owner_is_me is True and playlist.entries_tag == "s1"
    fake.calls.clear()
    await read(db, fake)
    assert not [c for c in fake.calls if c[0] == "playlist_items"]


async def test_a_refused_playlist_is_unreadable_and_retried_only_on_a_full_refresh(db):
    refused = spotipy.SpotifyException(403, -1, "Forbidden")
    fake = FakeSpotify(playlists=paged([sp_playlist("p2", owner="someone")]), playlist_items={"p2": refused})
    await read(db, fake)
    (playlist,) = await items(db, source="spotify", kind="playlist")
    assert playlist.readable is False and playlist.removed_at is None
    fake.calls.clear()
    await read(db, fake)
    assert not [c for c in fake.calls if c[0] == "playlist_items"]
    await read(db, fake, full=True)
    assert [c for c in fake.calls if c[0] == "playlist_items"]


async def test_a_missing_follow_scope_is_reported_and_no_artists_are_read(db):
    await mark_read(db)
    fake = FakeSpotify(follow=spotipy.SpotifyException(403, -1, "Insufficient client scope"))
    await read(db, fake)
    artists = await state(db, "spotify", "artist")
    assert artists.blocked_reason == "scope_missing" and artists.scope_probe_at is not None
    assert [c for c in fake.calls if c[0] == "follow"] == [("follow", None, 1)]
    fake.calls.clear()
    await read(db, fake)
    assert not [c for c in fake.calls if c[0] == "follow"]


async def test_followed_artists_are_read_once_the_probe_passes(db):
    await mark_read(db)
    fake = FakeSpotify(follow={"artists": {"items": [{"id": "a1", "name": "Miles Davis", "images": []}], "total": 1,
                                           "next": None, "cursors": {"after": None}}})
    await read(db, fake)
    assert [r.title for r in await items(db, source="spotify", kind="artist")] == ["Miles Davis"]


async def test_songs_without_isrcs_still_group_by_song_key(db):
    fake = FakeSpotify(tracks=paged([liked(1)]))
    await read(db, fake)
    (row,) = await items(db, source="spotify", kind="track")
    assert row.isrc is None and row.group_key == "song:song 1|artist"


async def test_embedded_album_tracks_become_entries(db):
    album = {"added_at": "2026-09-01T00:00:00Z",
             "album": {"id": "al1", "name": "Album", "artists": [{"name": "Artist"}], "release_date": "2004",
                       "total_tracks": 2, "images": [], "tracks": {"items": [sp_track(1), sp_track(2)], "total": 2}}}
    fake = FakeSpotify(albums=paged([album]))
    await read(db, fake)
    async with db() as session:
        saved = await session.scalar(select(SavedItem).where(SavedItem.kind == "album"))
        assert saved.track_count == 2 and saved.entries_fetched_at is not None


async def test_not_connected_blocks_every_kind_without_calls(db, monkeypatch):
    monkeypatch.setattr(sp, "get_client", lambda: None)
    await saved_spotify.run(db)
    assert (await state(db, "spotify", "track")).blocked_reason == "not_connected"


async def test_a_pause_after_the_last_page_resumes_at_the_final_check_without_rereading(db):
    pages = paged([liked(n) for n in range(1, 4)])
    fake = FakeSpotify(tracks=pages)

    def limited_head(limit=20, offset=0, market=None):
        if limit == 1:
            raise rate_limit_error()
        return FakeSpotify.current_user_saved_tracks(fake, limit, offset, market)

    fake.current_user_saved_tracks = limited_head
    await read(db, fake)
    assert json.loads((await state(db, "spotify", "track")).cursor) == {"offset": None}
    sp._rate_limited_until = 0.0
    fake = FakeSpotify(tracks=pages)
    await read(db, fake)
    assert [c for c in fake.calls if c[0] == "tracks"] == [("tracks", 0, 1)]
    assert (await state(db, "spotify", "track")).last_full_at is not None
