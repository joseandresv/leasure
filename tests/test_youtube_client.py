from services import youtube_client as yt


class FakeYTMusic:
    def __init__(self, album=None, playlist=None):
        self.album = album
        self.playlist = playlist

    def get_album(self, browse_id):
        return self.album

    def get_playlist(self, playlist_id, limit=500):
        return self.playlist


def _song(title, track_number=None, video_id=None):
    return {"videoId": video_id or title, "title": title, "artists": [{"name": "The Killers"}],
            "album": {"name": "Hot Fuss", "id": "MPREb_hot"}, "duration": "3:42", "trackNumber": track_number}


def test_album_track_without_a_number_takes_its_position(monkeypatch):
    songs = [_song("Jenny", 1), _song("Mr. Brightside", 2), _song("Smile Like You Mean It", 3), _song("Somebody")]
    monkeypatch.setattr(yt, "get_client", lambda: FakeYTMusic(album={"title": "Hot Fuss", "tracks": songs}))

    tracks = yt.get_album_tracks("MPREb_hot")["tracks"]

    assert [t["track_number"] for t in tracks] == [1, 2, 3, 4]


def test_an_album_playlist_is_numbered_by_position(monkeypatch):
    playlist = {"title": "Hot Fuss", "tracks": [_song("Jenny"), _song("Mr. Brightside"), _song("All These Things")]}
    monkeypatch.setattr(yt, "get_client", lambda: FakeYTMusic(playlist=playlist))

    tracks = yt.get_playlist_tracks("OLAK5uy_x")["tracks"]

    assert [(t["track_number"], t["total_tracks"]) for t in tracks] == [(1, 3), (2, 3), (3, 3)]
    assert tracks[0]["album_id"] == "MPREb_hot"


def test_an_ordinary_playlist_stays_unnumbered(monkeypatch):
    playlist = {"title": "Mix", "tracks": [_song("Jenny"), _song("Mr. Brightside")]}
    monkeypatch.setattr(yt, "get_client", lambda: FakeYTMusic(playlist=playlist))

    tracks = yt.get_playlist_tracks("PLx")["tracks"]

    assert [t["track_number"] for t in tracks] == [None, None]
