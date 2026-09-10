import pytest

from models import Track
from services import spotify_client
from services.spotify_client import get_album_tracks, get_liked_songs, get_playlist_tracks


def full_track(track_id: str, isrc: str | None = "GBAAA0000001") -> dict:
    track = {
        "id": track_id,
        "name": f"Track {track_id}",
        "type": "track",
        "uri": f"spotify:track:{track_id}",
        "artists": [{"id": "art1", "name": "New Order"}],
        "album": {"name": "Substance", "images": [{"url": "https://img/1.jpg"}]},
        "track_number": 1,
        "disc_number": 1,
        "duration_ms": 448_000,
    }
    if isrc:
        track["external_ids"] = {"isrc": isrc}
    return track


class FakeSpotify:
    """Only the calls spotify_client makes, recording how tracks() was batched."""

    def __init__(self, album_track_count: int = 3):
        self.album_track_count = album_track_count
        self.tracks_batches: list[list[str]] = []

    def album(self, album_id):
        return {
            "id": album_id,
            "name": "Substance",
            "artists": [{"name": "New Order"}],
            "images": [{"url": "https://img/album.jpg"}],
            "release_date": "1987",
            "uri": f"spotify:album:{album_id}",
            "genres": [],
            # The album payload carries simplified tracks: no external_ids.
            "tracks": {"items": [{k: v for k, v in full_track(f"t{i}").items() if k != "external_ids"}
                                 for i in range(self.album_track_count)]},
        }

    def tracks(self, ids):
        self.tracks_batches.append(list(ids))
        return {"tracks": [full_track(i, isrc=f"GBAAA{i}") for i in ids]}

    def playlist(self, playlist_id, fields=None):
        return {"name": "Mix", "images": [], "owner": {"display_name": "me"}}

    def playlist_items(self, playlist_id, limit=100, offset=0, additional_types=("track",)):
        return {"total": 1, "items": [{"track": full_track("p1")}], "next": None}

    def current_user_saved_tracks(self, limit=50, offset=0):
        return {"total": 1, "offset": offset, "limit": limit, "items": [{"track": full_track("l1")}]}


@pytest.fixture
def fake_spotify(monkeypatch):
    fake = FakeSpotify()
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake)
    return fake


def test_playlist_track_dict_carries_the_isrc(fake_spotify):
    assert get_playlist_tracks("pl1")["tracks"][0]["isrc"] == "GBAAA0000001"


def test_liked_song_dict_carries_the_isrc(fake_spotify):
    assert get_liked_songs()["tracks"][0]["isrc"] == "GBAAA0000001"


def test_track_dict_isrc_is_none_when_spotify_omits_external_ids(monkeypatch):
    assert spotify_client._parse_track(full_track("x1", isrc=None))["isrc"] is None


def test_album_tracks_fetch_the_isrc_the_album_payload_lacks(fake_spotify):
    tracks = get_album_tracks("al1")["tracks"]
    assert [t["isrc"] for t in tracks] == ["GBAAAt0", "GBAAAt1", "GBAAAt2"]


def test_album_isrc_lookup_is_batched_in_fifties(monkeypatch):
    fake = FakeSpotify(album_track_count=60)
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake)
    get_album_tracks("al1")
    assert [len(batch) for batch in fake.tracks_batches] == [50, 10]


def test_album_tracks_survive_a_failing_isrc_lookup(fake_spotify):
    def boom(ids):
        raise RuntimeError("429 rate limited")

    fake_spotify.tracks = boom
    tracks = get_album_tracks("al1")["tracks"]
    assert [t["isrc"] for t in tracks] == [None, None, None]


# --- scripts.reprobe ---


async def seed(db, **fields) -> int:
    async with db() as session:
        track = Track(title="Blue Monday", artist="New Order", status="done", **fields)
        session.add(track)
        await session.commit()
        return track.id


@pytest.mark.asyncio
async def test_reprobe_relabels_a_legacy_native_row(db):
    from scripts.reprobe import backfill

    track_id = await seed(db, quality="native", format="m4a", file_path="/library/New Order/01 - Blue Monday.m4a")
    await backfill(dry_run=False)

    async with db() as session:
        track = await session.get(Track, track_id)
        assert (track.quality, track.transcoded, track.is_lossless, track.verification) == (
            "aac_unknown", False, False, "unverified")


@pytest.mark.asyncio
async def test_reprobe_marks_a_legacy_lossy_flac_as_transcoded(db):
    from scripts.reprobe import backfill

    track_id = await seed(db, quality="flac_lossy", format="flac", file_path="/library/a/b.flac")
    await backfill(dry_run=False)

    async with db() as session:
        track = await session.get(Track, track_id)
        assert (track.transcoded, track.is_lossless) == (True, False)


@pytest.mark.asyncio
async def test_reprobe_leaves_rows_that_already_have_provenance_alone(db):
    from scripts.reprobe import backfill

    track_id = await seed(db, quality="aac_256", format="m4a", source_format_id="141",
                          transcoded=False, verification="ok", file_path="/library/a/b.m4a")
    await backfill(dry_run=False)

    async with db() as session:
        track = await session.get(Track, track_id)
        assert (track.quality, track.verification) == ("aac_256", "ok")


@pytest.mark.asyncio
async def test_reprobe_dry_run_writes_nothing(db):
    from scripts.reprobe import backfill

    track_id = await seed(db, quality="native", format="m4a", file_path="/library/a/b.m4a")
    counts = await backfill(dry_run=True)

    async with db() as session:
        track = await session.get(Track, track_id)
    assert counts["scanned"] == 1 and track.quality == "native" and track.verification is None


# --- unified dicts ---


@pytest.mark.asyncio
async def test_search_spotify_track_dict_carries_the_isrc(monkeypatch):
    from services import search

    class SearchOnly:
        def search(self, q, type, limit):
            return {"tracks": {"items": [full_track("s1")]}, "albums": {}, "artists": {}}

    monkeypatch.setattr(search.sp, "get_client", lambda: SearchOnly())
    results = await search.search_spotify("blue monday")
    assert results["tracks"][0]["isrc"] == "GBAAA0000001"


def test_unified_recent_track_dict_carries_the_isrc(monkeypatch):
    from services import music_aggregator as agg

    monkeypatch.setattr(agg.sp, "get_currently_playing", lambda: None)
    monkeypatch.setattr(agg.sp, "get_recently_played", lambda limit: [
        {"id": "r1", "name": "Blue Monday", "artist": "New Order", "isrc": "GBAAA0000001",
         "uri": "spotify:track:r1", "played_at": "2026-01-01T00:00:00Z"}])
    monkeypatch.setattr(agg.yt, "get_history", lambda limit: [])
    monkeypatch.setattr(agg.yt, "get_youtube_history", lambda limit: [])
    monkeypatch.setattr(agg, "_compute_entity_recency", lambda limit=50: {"albums": {}, "artists": {}})

    tracks = agg.get_unified_recent(limit=5, force=True)
    assert [t["isrc"] for t in tracks] == ["GBAAA0000001"]


@pytest.mark.asyncio
async def test_search_spotify_runs_the_spotipy_call_off_the_event_loop_thread(monkeypatch):
    import threading

    from services import search

    calls = []

    class ThreadRecording:
        def search(self, q, type, limit):
            calls.append(threading.current_thread() is threading.main_thread())
            return {"tracks": {"items": []}, "albums": {}, "artists": {}}

    monkeypatch.setattr(search.sp, "get_client", lambda: ThreadRecording())
    await search.search_spotify("blue monday")
    assert calls == [False]


@pytest.mark.asyncio
async def test_search_youtube_runs_the_ytmusicapi_calls_off_the_event_loop_thread(monkeypatch):
    import threading

    from services import search

    calls = []

    class ThreadRecording:
        def search(self, query, filter, limit):
            calls.append(threading.current_thread() is threading.main_thread())
            return []

    monkeypatch.setattr(search.yt, "get_client", lambda: ThreadRecording())
    await search.search_youtube("blue monday")
    assert calls == [False, False, False]


@pytest.mark.asyncio
async def test_search_results_download_button_carries_the_isrc(client, monkeypatch):
    from services import search

    class SearchOnly:
        def search(self, q, type, limit):
            return {"tracks": {"items": [full_track("s1")]}, "albums": {}, "artists": {}}

    monkeypatch.setattr(search.sp, "get_client", lambda: SearchOnly())
    monkeypatch.setattr(search.yt, "get_client", lambda: None)

    async def no_musicbrainz(query, limit=10):
        return {"albums": [], "tracks": [], "artists": []}

    monkeypatch.setattr(search, "search_musicbrainz", no_musicbrainz)

    resp = await client.get("/api/music/search", params={"q": "blue monday"})
    assert resp.status_code == 200
    assert '"isrc": "GBAAA0000001"' in resp.text


@pytest.mark.asyncio
async def test_deck_download_button_carries_the_isrc(client, monkeypatch):
    from routers import music

    monkeypatch.setattr(music, "get_unified_recent", lambda limit, refresh: [
        {"id": "r1", "name": "Blue Monday", "artist": "New Order", "album": "Substance",
         "isrc": "GBAAA0000001", "duration_ms": 448_000, "track_number": 1, "image_url": "",
         "sources": [{"provider": "spotify", "id": "r1", "uri": "spotify:track:r1", "artist_id": "art1"}]}])

    resp = await client.get("/api/music/recent")
    assert resp.status_code == 200
    assert '"isrc": "GBAAA0000001"' in resp.text
