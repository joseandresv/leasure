"""The YouTube Music refresh job of the Saved tab against scripted ytmusicapi answers (no network)."""
import pytest

from services import saved_youtube, youtube_client
from tests.saved_fakes import entries, items, state


def song(n: int) -> dict:
    return {"videoId": f"v{n}", "title": f"Song {n}", "artists": [{"name": "Artist"}], "album": {"name": "Album"},
            "duration": "3:20", "thumbnails": [{"url": "https://lh3.googleusercontent.com/x"}]}


class FakeYTMusic:
    def __init__(self, liked=None, albums=None, playlists=None, subscriptions=None, playlist_tracks=None):
        self.liked, self.albums = liked or [], albums or []
        self.playlists, self.subscriptions = playlists or [], subscriptions or []
        self.playlist_tracks = playlist_tracks or {}
        self.calls: list[tuple] = []

    def get_liked_songs(self, limit=100):
        self.calls.append(("liked", limit))
        return {"tracks": self.liked}

    def get_library_albums(self, limit=25, order=None):
        self.calls.append(("albums", limit, order))
        return self.albums

    def get_library_playlists(self, limit=25):
        self.calls.append(("playlists", limit))
        return self.playlists

    def get_library_subscriptions(self, limit=25, order=None):
        self.calls.append(("subscriptions", limit))
        return self.subscriptions

    def get_playlist(self, playlist_id, limit=100, related=False, suggestions_limit=0):
        self.calls.append(("playlist", playlist_id, limit))
        return {"title": playlist_id, "tracks": self.playlist_tracks.get(playlist_id, [])}


@pytest.fixture
def yt(monkeypatch, tmp_path):
    fake = FakeYTMusic()
    headers = tmp_path / "youtube_headers.json"
    headers.write_text("{}")
    monkeypatch.setattr(youtube_client, "HEADERS_PATH", headers)
    monkeypatch.setattr(youtube_client, "get_client", lambda: fake)
    monkeypatch.setattr(youtube_client, "is_connected", lambda: True)
    return fake


async def test_the_whole_library_is_asked_for_without_a_limit(db, yt):
    yt.liked = [song(1), {"title": "No video"}, song(2)]
    yt.albums = [{"browseId": "MPRE1", "title": "Album", "artists": [{"name": "Artist"}], "year": "2004"}]
    yt.subscriptions = [{"browseId": "UC1", "artist": "Artist"}]
    await saved_youtube.run(db)
    assert ("liked", None) in yt.calls and ("albums", None, "recently_added") in yt.calls
    assert ("playlists", None) in yt.calls and ("subscriptions", None) in yt.calls
    assert [r.source_id for r in await items(db, source="youtube", kind="track")] == ["v1", "v2"]
    assert [r.year for r in await items(db, source="youtube", kind="album")] == [2004]


async def test_an_empty_answer_with_stale_cookies_removes_nothing_and_reports_not_connected(db, yt, monkeypatch):
    yt.liked = [song(n) for n in range(1, 6)]
    await saved_youtube.run(db)
    yt.liked = []
    monkeypatch.setattr(youtube_client, "is_connected", lambda: False)
    await saved_youtube.run(db)
    assert all(r.removed_at is None for r in await items(db, source="youtube", kind="track"))
    assert (await state(db, "youtube", "track")).blocked_reason == "not_connected"


async def test_a_removal_needs_two_passes_that_agree(db, yt):
    yt.liked = [song(n) for n in range(1, 31)]
    await saved_youtube.run(db)
    yt.liked = [song(n) for n in range(2, 31)]
    await saved_youtube.run(db)
    assert not [r for r in await items(db, source="youtube", kind="track") if r.removed_at]
    await saved_youtube.run(db)
    assert [r.source_id for r in await items(db, source="youtube", kind="track") if r.removed_at] == ["v1"]


async def test_a_removed_song_comes_back_when_it_reappears(db, yt):
    yt.liked = [song(n) for n in range(1, 31)]
    await saved_youtube.run(db)
    yt.liked = [song(n) for n in range(2, 31)]
    await saved_youtube.run(db)
    await saved_youtube.run(db)
    yt.liked = [song(n) for n in range(1, 31)]
    await saved_youtube.run(db)
    assert not [r for r in await items(db, source="youtube", kind="track") if r.removed_at]


async def test_playlists_with_unknown_counts_are_read_at_most_ten_per_run(db, yt):
    yt.playlists = [{"playlistId": "LM", "title": "Liked Music"}] + [
        {"playlistId": f"PL{n}", "title": f"List {n}", "count": "many"} for n in range(12)]
    yt.playlist_tracks = {"PL0": [song(1)]}
    await saved_youtube.run(db)
    assert len([c for c in yt.calls if c[0] == "playlist"]) == saved_youtube.PLAYLISTS_PER_RUN
    playlists = await items(db, source="youtube", kind="playlist")
    assert "LM" not in {p.source_id for p in playlists}
    first = next(p for p in playlists if p.source_id == "PL0")
    assert [e.source_id for e in await entries(db, first.id)] == ["v1"]


async def test_without_headers_every_kind_is_not_connected(db, monkeypatch, tmp_path):
    monkeypatch.setattr(youtube_client, "HEADERS_PATH", tmp_path / "none.json")
    await saved_youtube.run(db)
    assert (await state(db, "youtube", "artist")).blocked_reason == "not_connected"
