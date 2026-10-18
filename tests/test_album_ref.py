import pytest

from services import music_aggregator as agg
from services import youtube_client as yt


def _recent(monkeypatch, sp_recent=(), yt_history=(), yt_plain=()):
    monkeypatch.setattr(agg.sp, "get_currently_playing", lambda: None)
    monkeypatch.setattr(agg.sp, "get_recently_played", lambda limit: list(sp_recent))
    monkeypatch.setattr(agg.yt, "get_history", lambda limit: list(yt_history))
    monkeypatch.setattr(agg.yt, "get_youtube_history", lambda limit: list(yt_plain))
    return agg.get_unified_recent(limit=10, force=True)


SP_TRACK = {"id": "s1", "name": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss",
            "album_id": "spAlbum1", "uri": "spotify:track:s1", "played_at": "2026-01-01T00:00:00Z"}
YTM_TRACK = {"id": "v1", "name": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss",
             "album_id": "MPREb_hot", "played": "Today"}


def test_spotify_recent_track_carries_its_album_ref(monkeypatch):
    tracks = _recent(monkeypatch, sp_recent=[SP_TRACK])
    assert tracks[0]["album_ref"] == {"provider": "spotify", "id": "spAlbum1"}


def test_youtube_music_history_track_carries_its_album_ref(monkeypatch):
    tracks = _recent(monkeypatch, yt_history=[YTM_TRACK])
    assert tracks[0]["album_ref"] == {"provider": "youtube", "id": "MPREb_hot"}


def test_merged_track_prefers_the_youtube_album(monkeypatch):
    tracks = _recent(monkeypatch, sp_recent=[SP_TRACK], yt_history=[YTM_TRACK])
    assert [t["album_ref"] for t in tracks] == [{"provider": "youtube", "id": "MPREb_hot"}]


def test_plain_youtube_track_has_no_album_ref(monkeypatch):
    tracks = _recent(monkeypatch, yt_plain=[{"id": "w1", "name": "The Killers - Mr. Brightside", "artist": "KillersVEVO"}])
    assert tracks[0]["album_ref"] is None


def test_youtube_music_history_keeps_the_album_browse_id(monkeypatch):
    class History:
        def get_history(self):
            return [{"videoId": "v1", "title": "Jenny", "artists": [{"name": "The Killers"}],
                     "album": {"name": "Hot Fuss", "id": "MPREb_hot"}, "duration": "3:42", "played": "Today"}]

    monkeypatch.setattr(yt, "get_client", lambda: History())
    assert yt.get_history()[0]["album_id"] == "MPREb_hot"


class FakeSearch:
    def __init__(self, songs=(), albums=()):
        self.results = {"songs": list(songs), "albums": list(albums)}

    def search(self, query, filter=None, limit=20):
        return self.results[filter]


def _song(title, artist, album_id, seconds=222):
    return {"videoId": f"vid-{title}", "title": title, "artists": [{"name": artist}],
            "album": {"name": "Hot Fuss", "id": album_id}, "duration_seconds": seconds}


HOT_FUSS = {
    "album": {"name": "Hot Fuss", "artist": "The Killers", "album_artist": "The Killers",
              "image_url": None, "year": "2004", "total_tracks": 1, "total_discs": None},
    "tracks": [{"id": "vid-Mr. Brightside", "name": "Mr. Brightside", "artist": "The Killers",
                "track_number": 2, "duration_ms": 222000, "album_artist": "The Killers", "year": 2004}],
}


def _serve_album(monkeypatch, client):
    from routers import music as music_router

    opened = []
    monkeypatch.setattr(agg.yt, "get_client", lambda: client)
    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: opened.append(bid) or HOT_FUSS)
    return opened


@pytest.mark.asyncio
async def test_resolve_opens_the_album_of_a_plain_youtube_video(client, monkeypatch):
    search = FakeSearch(songs=[_song("Mr. Brightside (Live)", "The Killers", "MPREb_live"),
                               _song("Mr. Brightside", "The Killers", "MPREb_hot")])
    opened = _serve_album(monkeypatch, search)

    resp = await client.get("/api/music/album/resolve",
                            params={"title": "The Killers - Mr. Brightside (Official Music Video)", "artist": "KillersVEVO"})

    assert resp.status_code == 200
    assert opened == ["MPREb_hot"]
    assert "/api/music/download/album/youtube/MPREb_hot" in resp.text


@pytest.mark.asyncio
async def test_resolve_searches_albums_when_the_album_name_is_known(client, monkeypatch):
    search = FakeSearch(albums=[{"browseId": "MPREb_hot", "title": "Hot Fuss", "artists": [{"name": "The Killers"}]}])
    opened = _serve_album(monkeypatch, search)

    await client.get("/api/music/album/resolve",
                     params={"title": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss"})

    assert opened == ["MPREb_hot"]


@pytest.mark.asyncio
async def test_resolve_without_a_confident_match_says_so(client, monkeypatch):
    opened = _serve_album(monkeypatch, FakeSearch(songs=[_song("Somebody Told Me", "The Killers", "MPREb_hot")]))

    resp = await client.get("/api/music/album/resolve", params={"title": "Blue Monday", "artist": "New Order"})

    assert resp.status_code == 200
    assert opened == []
    assert "No matching album" in resp.text


class AnonymousYTMusic:
    def get_album(self, browse_id):
        return {"title": "Hot Fuss", "artists": [{"name": "The Killers"}], "year": "2004", "tracks": [
            {"videoId": "vid-anon", "title": "Mr. Brightside", "artists": [{"name": "The Killers"}],
             "trackNumber": 2, "duration": "3:42"}]}


@pytest.fixture
def no_saved_login(monkeypatch):
    import ytmusicapi

    monkeypatch.setattr(yt, "get_client", lambda: None)
    monkeypatch.setattr(ytmusicapi, "YTMusic", AnonymousYTMusic)


@pytest.mark.asyncio
async def test_youtube_album_opens_without_a_saved_login(client, no_saved_login):
    resp = await client.get("/api/music/album/youtube/MPREb_hot")

    assert "Mr. Brightside" in resp.text and "/api/music/download/album/youtube/MPREb_hot" in resp.text


@pytest.mark.asyncio
async def test_youtube_album_downloads_without_a_saved_login(client, no_saved_login, monkeypatch):
    from routers import music as music_router

    queued = []

    async def enqueue(track_id):
        queued.append(track_id)

    monkeypatch.setattr(music_router.download_worker, "enqueue", enqueue)

    resp = await client.post("/api/music/download/album/youtube/MPREb_hot", data={"format": "mp3"})

    assert resp.json()["tracks_queued"] == 1 and len(queued) == 1


class RecordingSearch(FakeSearch):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queries = []

    def search(self, query, filter=None, limit=20):
        self.queries.append((query, filter))
        return super().search(query, filter, limit)


def _album(browse_id, title, artist):
    return {"browseId": browse_id, "title": title, "artists": [{"name": artist}]}


def test_full_album_upload_behind_an_invisible_mark_is_found_by_album_name(monkeypatch):
    search = RecordingSearch(albums=[_album("MPREb_legacy", "Legacy Of You", "Toshiki Kadomatsu")])
    monkeypatch.setattr(agg.yt, "browse_client", lambda: search)

    found = agg.find_youtube_album("Toshiki Kadomatsu ‎- Legacy Of You (1990) Full Album", "CityPopArchive")

    assert (found, search.queries) == ("MPREb_legacy", [("Toshiki Kadomatsu Legacy Of You", "albums")])


def test_bracketed_full_album_marker_searches_the_album_with_one_call(monkeypatch):
    search = RecordingSearch(albums=[_album("MPREb_relax", "Relaxation Music", "Takumi Yoneyama")])
    monkeypatch.setattr(agg.yt, "browse_client", lambda: search)

    found = agg.find_youtube_album("Takumi Yoneyama (米山拓巳) - Relaxation Music…[Full Album]", "Ambient Uploads")

    assert (found, [f for _, f in search.queries]) == ("MPREb_relax", ["albums"])


def test_an_em_dash_still_splits_artist_from_song(monkeypatch):
    search = RecordingSearch(songs=[_song("Mr. Brightside", "The Killers", "MPREb_hot")])
    monkeypatch.setattr(agg.yt, "browse_client", lambda: search)

    assert agg.find_youtube_album("The Killers — Mr. Brightside", "KillersVEVO") == "MPREb_hot"
