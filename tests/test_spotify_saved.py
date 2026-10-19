"""Saved-library reads in services/spotify_client.py against a scripted spotipy stand-in (no network)."""
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
import spotipy

from config import settings
from services import spotify_client as sp


def track(n: int, **extra) -> dict:
    return {"id": f"t{n}", "uri": f"spotify:track:t{n}", "name": f"Song {n}", "type": "track",
            "artists": [{"id": "ar1", "name": "Artist"}], "duration_ms": 200000, "track_number": 1,
            "album": {"id": "al1", "name": "Album", "artists": [{"name": "Artist"}], "release_date": "2004",
                      "images": [{"url": "https://i.scdn.co/image/abc"}]}, **extra}


def page(items: list, total: int, offset: int, more: bool = True) -> dict:
    return {"items": items, "total": total, "offset": offset, "next": "https://api.spotify.com/next" if more else None}


def rate_limit_error():
    return spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "7200"})


class FakeSpotify:
    """Answers library calls from scripted pages keyed by offset (or cursor); records every call."""

    def __init__(self, pages: dict | None = None, *, follow=None, playlist_error=None):
        self.pages = pages or {}
        self.follow = follow or {}
        self.playlist_error = playlist_error
        self.calls: list[tuple] = []

    def _answer(self, name, key):
        self.calls.append((name, key))
        answer = self.pages[name][key] if isinstance(self.pages.get(name), dict) else self.pages.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def current_user_saved_tracks(self, limit=20, offset=0, market=None):
        return self._answer("tracks", offset)

    def current_user_saved_albums(self, limit=20, offset=0, market=None):
        return self._answer("albums", offset)

    def current_user_playlists(self, limit=50, offset=0):
        return self._answer("playlists", offset)

    def playlist_items(self, playlist_id, fields=None, limit=50, offset=0, market=None, additional_types=()):
        self.calls.append(("playlist_items", playlist_id, fields, additional_types))
        if self.playlist_error:
            raise self.playlist_error
        return page([{"added_at": "2026-01-01T00:00:00Z", "item": track(1)}], 1, offset, more=False)

    def current_user_followed_artists(self, limit=20, after=None):
        self.calls.append(("follow", after, limit))
        answer = self.follow[after]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def no_window(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_rate_limited_until", 0.0)
    monkeypatch.setattr(sp, "RATE_LIMIT_PATH", tmp_path / "spotify_rate_limit.json")


def test_liked_songs_accept_item_and_track_shapes_and_count_placeholders():
    raw = page([{"added_at": "2026-10-01T10:00:00Z", "item": track(1)},
                {"added_at": "2026-09-01T10:00:00Z", "track": track(2, external_ids={"isrc": "USUM70409983"})},
                {"added_at": "2026-08-01T10:00:00Z", "track": None},
                {"added_at": "2026-07-01T10:00:00Z", "is_local": True,
                 "track": {"id": None, "uri": "spotify:local:a:b:c:200", "name": "Home demo", "album": None}},
                {"added_at": "2026-06-01T10:00:00Z", "item": {"id": "e1", "type": "episode"}}], total=5, offset=0, more=False)

    got = sp.saved_tracks_page(client=FakeSpotify({"tracks": {0: raw}}))

    assert [t["id"] for t in got.items] == ["t1", "t2", None]
    assert got.items[0]["isrc"] is None and got.items[1]["isrc"] == "USUM70409983"
    assert got.items[2]["is_local"] and got.items[2]["uri"].startswith("spotify:local:")
    assert (got.placeholders, got.raw_count, got.next) == (2, 5, None)
    assert got.items[0]["added_at"] == "2026-10-01T10:00:00Z" and got.items[0]["album_image_url"].startswith("https://")


def test_fetch_saved_tracks_pages_by_items_received_until_next_is_null():
    pages = {0: page([{"track": track(i)} for i in range(50)], 120, 0),
             50: page([{"track": track(i)} for i in range(50, 99)] + [{"track": None}], 120, 50),
             100: page([{"track": track(i)} for i in range(100, 120)], 120, 100, more=False)}
    fake = FakeSpotify({"tracks": pages})

    listing = sp.fetch_saved_tracks(client=fake)

    assert listing.complete and listing.resume is None and listing.total == 120
    assert len(listing.items) == 119 and listing.placeholders == 1
    assert [c[1] for c in fake.calls] == [0, 50, 100]


def test_fetch_stops_on_an_empty_page_and_at_offset_total():
    empty = FakeSpotify({"tracks": {0: page([{"track": track(1)}], 10, 0), 1: page([], 10, 1)}})
    at_total = FakeSpotify({"tracks": {0: page([{"track": track(1)}, {"track": track(2)}], 2, 0)}})

    assert sp.fetch_saved_tracks(client=empty).complete and len(empty.calls) == 2
    assert sp.fetch_saved_tracks(client=at_total).complete and len(at_total.calls) == 1


def test_a_list_that_never_ends_stops_at_the_page_cap():
    same = page([{"track": track(1)}], 10_000, 0)

    class Endless(FakeSpotify):
        def current_user_saved_tracks(self, limit=20, offset=0, market=None):
            self.calls.append(("tracks", offset))
            return same

    fake = Endless()
    listing = sp.fetch_saved_tracks(client=fake, page_cap=4)

    assert (listing.complete, listing.stopped, len(fake.calls)) == (False, "page_cap", 4)


def test_a_429_on_page_two_stops_with_a_resume_point_and_sends_nothing_more(tmp_path):
    fake = FakeSpotify({"tracks": {0: page([{"track": track(i)} for i in range(50)], 200, 0),
                                   50: rate_limit_error()}})

    listing = sp.fetch_saved_tracks(client=fake)

    assert (listing.stopped, listing.resume, len(listing.items)) == ("rate_limited", 50, 50)
    assert sp.is_rate_limited() and len(fake.calls) == 2
    assert sp.fetch_saved_tracks(client=fake) is None and len(fake.calls) == 2


def test_the_rate_limit_window_survives_a_restart(tmp_path):
    sp._note_rate_limit(rate_limit_error())

    saved = json.loads(sp.RATE_LIMIT_PATH.read_text())["until"]
    restored = sp._restored_rate_limit_until()

    assert saved == pytest.approx(time.time() + 7200, abs=5)
    assert restored - time.monotonic() == pytest.approx(7200, abs=5)


@pytest.mark.parametrize("content", ['{"until": 1}', "not json", '{"other": 5}'])
def test_an_expired_or_unreadable_window_is_not_restored(content):
    sp.RATE_LIMIT_PATH.write_text(content)

    assert sp._restored_rate_limit_until() == 0.0


def test_saved_albums_report_embedded_tracks_only_when_present():
    with_tracks = {"added_at": "2026-01-01T00:00:00Z",
                   "album": {"id": "al1", "name": "Album", "artists": [{"name": "Band"}], "total_tracks": 2,
                             "release_date": "1999-05-01", "images": [],
                             "tracks": {"items": [track(1, album=None), track(2, album=None)], "total": 2}}}
    without = {"added_at": "2026-01-02T00:00:00Z", "album": {"id": "al2", "name": "Other", "artists": []}}
    fake = FakeSpotify({"albums": {0: page([with_tracks, without, {"album": None}], 3, 0, more=False)}})

    first, second = sp.saved_albums_page(client=fake).items

    assert [t["album_id"] for t in first["tracks"]] == ["al1", "al1"] and first["year"] == 1999
    assert first["album_artist"] == "Band" and second["tracks"] is None


@pytest.mark.parametrize("count", [{"tracks": {"total": 7}}, {"items": {"total": 7}}])
def test_playlist_counts_are_read_under_tracks_or_items(count):
    raw = {"id": "p1", "name": "Mix", "owner": {"id": "me", "display_name": "Me"}, "snapshot_id": "s1", **count}
    fake = FakeSpotify({"playlists": {0: page([raw], 1, 0, more=False)}})

    (got,) = sp.user_playlists_page(client=fake).items

    assert (got["track_count"], got["owner_id"], got["snapshot_id"]) == (7, "me", "s1")


def test_playlist_items_send_no_fields_filter():
    fake = FakeSpotify()

    listing = sp.fetch_playlist_items("p1", client=fake)

    assert listing.complete and [t["id"] for t in listing.items] == ["t1"]
    assert fake.calls == [("playlist_items", "p1", None, ("track",))]


@pytest.mark.parametrize("status", [403, 404])
def test_a_refused_playlist_is_reported_unreadable(status):
    fake = FakeSpotify(playlist_error=spotipy.SpotifyException(status, -1, "Forbidden"))

    listing = sp.fetch_playlist_items("p1", client=fake)

    assert (listing.stopped, listing.status, listing.complete) == ("unreadable", status, False)


def artists_answer(ids: list[str], after: str | None, more: bool) -> dict:
    return {"artists": {"items": [{"id": i, "name": i.upper(), "genres": ["rock"]} for i in ids], "total": 3,
                        "next": "https://api.spotify.com/next" if more else None, "cursors": {"after": after}}}


def test_followed_artists_follow_the_cursor():
    fake = FakeSpotify(follow={None: artists_answer(["a1", "a2"], "a2", True), "a2": artists_answer(["a3"], None, False)})

    listing = sp.fetch_followed_artists(client=fake)

    assert [a["id"] for a in listing.items] == ["a1", "a2", "a3"] and listing.complete
    assert [c[1] for c in fake.calls] == [None, "a2"]


def test_follow_probe_reads_scope_missing_from_a_403():
    scope_error = spotipy.SpotifyException(403, -1, "https://api.spotify.com/v1/me/following:\n Insufficient client scope")
    fake = FakeSpotify(follow={None: scope_error})

    assert sp.follow_scope_status(client=fake) == "scope_missing" and len(fake.calls) == 1
    assert sp.fetch_followed_artists(client=fake).stopped == "scope_missing"


def test_follow_probe_granted_and_rate_limited():
    assert sp.follow_scope_status(client=FakeSpotify(follow={None: artists_answer([], None, False)})) == "granted"
    assert sp.follow_scope_status(client=FakeSpotify(follow={None: rate_limit_error()})) is None
    assert sp.is_rate_limited()


@pytest.fixture
def spotify_app(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "spotify_client_id", "cid")
    monkeypatch.setattr(settings, "spotify_client_secret", "sec")
    monkeypatch.setattr(sp, "CACHE_PATH", str(tmp_path / ".spotify_cache"))
    return tmp_path / ".spotify_cache"


def test_the_authorize_url_asks_for_the_follow_scope(spotify_app):
    scopes = parse_qs(urlparse(sp.get_auth_url(state="s")).query)["scope"][0].split()

    assert sp.FOLLOW_SCOPE in scopes and set(sp.SCOPES.split()) <= set(scopes)


@pytest.mark.parametrize("cached_scope", [sp.SCOPES, sp.AUTH_SCOPES])
def test_a_token_refreshed_by_the_base_scope_manager_stays_connected(spotify_app, cached_scope):
    # spotipy writes the manager's requested scope into the cache on every refresh, so after one
    # refresh the cache says SCOPES even when the owner granted user-follow-read.
    spotify_app.write_text(json.dumps({"access_token": "x", "refresh_token": "r", "token_type": "Bearer",
                                       "expires_in": 3600, "expires_at": int(time.time()) + 3600, "scope": cached_scope}))

    assert sp.get_client() is not None


def test_pacer_keeps_the_gap_and_the_burst_limit():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    pacer = sp.SyncPacer(gap_s=3.0, jitter_s=1.5, burst=8, window_s=30.0, clock=lambda: now[0], sleep=sleep,
                         jitter=lambda low, high: 0.0)
    sent = []
    for _ in range(10):
        pacer.wait()
        sent.append(now[0])

    assert all(b - a >= 3.0 for a, b in zip(sent, sent[1:], strict=False))
    assert all(sum(1 for t in sent if start <= t < start + 30) <= 8 for start in sent)
    assert sent[8] == 30.0
