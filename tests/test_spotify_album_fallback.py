import json
import shutil
import time
from pathlib import Path

import pytest
import spotipy
from sqlalchemy import func, select

from models import Track
from services import music_aggregator as agg
from services import secret_file, spotify_albums, spotify_client

ALBUM_ID = "4OHNH3sDzIxnmUADXzv2kT"
URL = f"/api/music/album/spotify/{ALBUM_ID}"
HOT_FUSS_PARAMS = {"name": "Hot Fuss", "artist": "The Killers"}

SPOTIFY_ALBUM = {
    "album": {"id": ALBUM_ID, "name": "Hot Fuss", "artist": "The Killers", "image_url": None,
              "release_date": "2004-06-07", "uri": f"spotify:album:{ALBUM_ID}", "genres": [],
              "album_artist": "The Killers", "total_tracks": 2, "total_discs": 1},
    "tracks": [
        {"id": "t1", "name": "Jenny Was a Friend of Mine", "artist": "The Killers", "artist_id": "a1",
         "track_number": 1, "disc_number": 1, "duration_ms": 244000, "uri": "spotify:track:t1",
         "isrc": "USIR10400001", "album_artist": "The Killers", "year": 2004, "total_tracks": 2},
        {"id": "t2", "name": "Mr. Brightside", "artist": "The Killers", "artist_id": "a1",
         "track_number": 2, "disc_number": 1, "duration_ms": 222000, "uri": "spotify:track:t2",
         "isrc": "USIR10400002", "album_artist": "The Killers", "year": 2004, "total_tracks": 2},
    ],
}

YT_HOT_FUSS = {
    "album": {"name": "Hot Fuss", "artist": "The Killers", "album_artist": "The Killers",
              "image_url": None, "year": "2004", "total_tracks": 2, "total_discs": None},
    "tracks": [
        {"id": "vid-jenny", "name": "Jenny Was a Friend of Mine", "artist": "The Killers", "track_number": 1,
         "duration_ms": 244000, "album_artist": "The Killers", "year": 2004},
        {"id": "vid-bright", "name": "Mr. Brightside", "artist": "The Killers", "track_number": 2,
         "duration_ms": 222000, "album_artist": "The Killers", "year": 2004},
    ],
}


class FakeSearch:
    def __init__(self, albums=()):
        self.albums = list(albums)
        self.queries: list[str] = []

    def search(self, query, filter=None, limit=20):
        self.queries.append(query)
        return self.albums if filter == "albums" else []


HOT_FUSS_RESULT = {"browseId": "MPREb_hot", "title": "Hot Fuss", "artists": [{"name": "The Killers"}]}


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    monkeypatch.setattr(spotify_client, "_rate_limited_until", 0.0)
    monkeypatch.setattr(agg, "_albums_cache", {"data": None})
    monkeypatch.setattr(agg, "_recent_cache", {"ts": 0.0, "limit": 0, "data": None})
    shutil.rmtree(spotify_albums.cache_dir(), ignore_errors=True)
    yield
    shutil.rmtree(spotify_albums.cache_dir(), ignore_errors=True)


@pytest.fixture
def spotify_calls(monkeypatch):
    calls: list[str] = []

    def album_tracks(album_id):
        calls.append(album_id)
        return json.loads(json.dumps(SPOTIFY_ALBUM))

    monkeypatch.setattr(spotify_client, "get_album_tracks", album_tracks)
    return calls


@pytest.fixture
def no_spotify(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Spotify was called")

    monkeypatch.setattr(spotify_client, "get_album_tracks", refuse)
    monkeypatch.setattr(spotify_client, "get_client", refuse)


@pytest.fixture
def rate_limited(monkeypatch):
    monkeypatch.setattr(spotify_client, "_rate_limited_until", time.monotonic() + 7200)


@pytest.fixture
def youtube(monkeypatch):
    """YouTube Music finds Hot Fuss; returns the search fake and the list of albums opened."""
    from routers import music as music_router

    search = FakeSearch(albums=[HOT_FUSS_RESULT])
    opened: list[str] = []
    monkeypatch.setattr(agg.yt, "browse_client", lambda: search)
    monkeypatch.setattr(music_router.yt, "get_album_tracks",
                        lambda bid: opened.append(bid) or json.loads(json.dumps(YT_HOT_FUSS)))
    return search, opened


@pytest.fixture
def youtube_finds_nothing(monkeypatch):
    search = FakeSearch()
    monkeypatch.setattr(agg.yt, "browse_client", lambda: search)
    return search


class Rendered(list):
    def __init__(self):
        super().__init__()
        self.names: list[str] = []


@pytest.fixture
def rendered(monkeypatch):
    """The context of every album-detail template the music router renders."""
    from routers import music as music_router

    contexts = Rendered()
    real = music_router.templates.TemplateResponse

    def spy(*args, **kwargs):
        contexts.append(kwargs["context"])
        contexts.names.append(kwargs["name"])
        return real(*args, **kwargs)

    monkeypatch.setattr(music_router.templates, "TemplateResponse", spy)
    return contexts


def write_cache_entry(age_s: float) -> None:
    path = spotify_albums.cache_dir() / f"{ALBUM_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": time.time() - age_s, "data": SPOTIFY_ALBUM}), encoding="utf-8")


async def seed(db, **fields) -> int:
    values = {"title": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss",
              "album_artist": "The Killers", "status": "done", "quality": "aac_256", "format": "m4a",
              "file_path": "/tmp/x.m4a", **fields}
    async with db() as session:
        track = Track(**values)
        session.add(track)
        await session.commit()
        return track.id


def status_of(context: dict, name: str):
    return next(t for t in context["tracks"] if t["name"] == name)["download_status"]


# --- the on-disk album cache ---


async def test_a_cached_album_opens_without_asking_spotify_again(client, db, spotify_calls):
    first = await client.get(URL)
    second = await client.get(URL)

    assert "Mr. Brightside" in first.text and "Mr. Brightside" in second.text
    assert spotify_calls == [ALBUM_ID]


async def test_a_stale_cache_entry_is_served_while_spotify_is_rate_limited(client, db, rate_limited, no_spotify):
    write_cache_entry(age_s=40 * 86400)

    resp = await client.get(URL)

    assert "Mr. Brightside" in resp.text and "rate-limiting" not in resp.text


async def test_a_stale_cache_entry_is_served_when_spotify_errors(client, db, monkeypatch):
    write_cache_entry(age_s=40 * 86400)

    def boom(album_id):
        raise spotipy.SpotifyException(500, -1, "server error")

    monkeypatch.setattr(spotify_client, "get_album_tracks", boom)

    resp = await client.get(URL)

    assert resp.status_code == 200 and "Mr. Brightside" in resp.text


def test_a_stale_cache_entry_is_refreshed_when_spotify_answers(spotify_calls):
    write_cache_entry(age_s=40 * 86400)

    spotify_albums.get_album(ALBUM_ID)

    entry = json.loads((spotify_albums.cache_dir() / f"{ALBUM_ID}.json").read_text(encoding="utf-8"))
    assert spotify_calls == [ALBUM_ID] and time.time() - entry["fetched_at"] < 60


@pytest.mark.parametrize("album_id", ["../../leasure", "..", "abc/def", "x" * 23, "4OHNH3sDzIxnmUADXzv2k.", ""])
def test_an_invalid_album_id_never_touches_the_filesystem(monkeypatch, spotify_calls, album_id):
    def refuse(*args, **kwargs):
        raise AssertionError("filesystem touched")

    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "mkdir", refuse)
    monkeypatch.setattr(secret_file, "write_atomic", refuse)

    assert spotify_albums.get_album(album_id) is None
    assert spotify_calls == []


# --- falling back to YouTube Music ---


async def test_rate_limited_album_comes_from_youtube_music_by_name_and_artist(
        client, db, rate_limited, no_spotify, youtube, rendered):
    search, opened = youtube

    resp = await client.get(URL, params=HOT_FUSS_PARAMS)

    assert opened == ["MPREb_hot"]
    assert "/api/music/download/album/youtube/MPREb_hot" in resp.text
    assert rendered[-1]["provider"] == "youtube"
    assert rendered[-1]["notice"] == ("Spotify is rate-limiting Leasure (try again in ~2 h), "
                                      "so this album comes from YouTube Music.")


async def test_without_params_the_cached_album_list_names_the_album(client, db, monkeypatch, youtube, rendered):
    monkeypatch.setattr(agg.sp, "get_saved_albums", lambda limit, offset: {"albums": [
        {"id": ALBUM_ID, "name": "Hot Fuss", "artist": "The Killers", "image_url": None,
         "total_tracks": 11, "release_date": "2004", "uri": f"spotify:album:{ALBUM_ID}"}]})
    monkeypatch.setattr(agg.yt, "get_library_albums", lambda limit: [])
    monkeypatch.setattr(agg, "_compute_entity_recency", lambda: {"albums": {}})
    agg.get_unified_albums()
    monkeypatch.setattr(spotify_client, "_rate_limited_until", time.monotonic() + 7200)
    monkeypatch.setattr(spotify_client, "get_album_tracks", lambda album_id: pytest.fail("Spotify was called"))
    search, opened = youtube

    await client.get(URL)

    assert search.queries == ["The Killers Hot Fuss"] and opened == ["MPREb_hot"]


async def test_without_params_the_recent_tracks_name_the_album(client, db, rate_limited, no_spotify, youtube):
    agg._recent_cache.update({"ts": time.time(), "limit": 50, "data": [
        {"name": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss", "album_artist": "The Killers",
         "album_ref": {"provider": "spotify", "id": ALBUM_ID}}]})
    search, opened = youtube

    await client.get(URL)

    assert search.queries == ["The Killers Hot Fuss"] and opened == ["MPREb_hot"]


# --- the library view ---


async def test_with_no_youtube_match_the_album_opens_from_the_library(
        client, db, rate_limited, no_spotify, youtube_finds_nothing, rendered):
    kept = await seed(db, track_number=2, spotify_uri="spotify:track:t2")
    await seed(db, title="Jenny Was a Friend of Mine", track_number=1, youtube_id="vid-jenny")
    await seed(db, title="Somebody Told Me", album="Sam's Town")

    resp = await client.get(URL, params=HOT_FUSS_PARAMS)

    context = rendered[-1]
    assert resp.status_code == 200 and rendered.names[-1] == "partials/album_view.html"
    assert context["provider"] == "library" and context["album_id"] == ALBUM_ID
    assert [t["name"] for t in context["tracks"]] == ["Jenny Was a Friend of Mine", "Mr. Brightside"]
    assert context["tracks"][1]["db"]["id"] == kept and context["tracks"][1]["download_status"] == "done"
    assert context["notice"].endswith("so this shows your library's copy of this album.")
    assert context["retry_url"] == f"{URL}?name=Hot+Fuss&artist=The+Killers"


async def test_a_deluxe_spotify_album_opens_the_librarys_standard_copy_and_says_so(
        client, db, rate_limited, no_spotify, youtube_finds_nothing, rendered):
    await seed(db)

    await client.get(URL, params={"name": "Hot Fuss (Deluxe Edition)", "artist": "The Killers"})

    assert rendered[-1]["notice"].endswith("Showing your library's 'Hot Fuss'.")


async def test_an_unanswered_youtube_album_opens_the_librarys_copy(client, db, monkeypatch, rendered):
    from routers import music as music_router

    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: None)
    agg._albums_cache["data"] = [{"name": "Hot Fuss", "artist": "The Killers",
                                  "sources": [{"provider": "youtube", "id": "MPREb_hot"}]}]
    await seed(db)

    resp = await client.get("/api/music/album/youtube/MPREb_hot")

    assert resp.status_code == 200 and rendered.names[-1] == "partials/album_view.html"
    assert rendered[-1]["notice"] == "YouTube Music did not answer, so this shows your library's copy."
    assert [t["name"] for t in rendered[-1]["tracks"]] == ["Mr. Brightside"]


async def test_a_deck_album_youtube_cannot_find_opens_from_the_library(client, db, monkeypatch, rendered):
    from routers import music as music_router

    monkeypatch.setattr(music_router, "find_youtube_album", lambda *args: None)
    await seed(db)

    await client.get("/api/music/album/resolve", params={"title": "Mr. Brightside", "artist": "The Killers",
                                                         "album": "Hot Fuss"})

    assert rendered.names[-1] == "partials/album_view.html" and rendered[-1]["notice"] == "Found in your library."


async def test_nothing_found_anywhere_says_spotify_is_rate_limiting(
        client, db, rate_limited, no_spotify, youtube_finds_nothing):
    resp = await client.get(URL, params=HOT_FUSS_PARAMS)

    assert "Spotify is rate-limiting Leasure right now (try again in ~2 h)" in resp.text


async def test_the_library_provider_reads_only_the_database(client, db, no_spotify, monkeypatch, rendered):
    monkeypatch.setattr(agg.yt, "browse_client", lambda: pytest.fail("YouTube was called"))
    await seed(db)

    await client.get(f"/api/music/album/library/{ALBUM_ID}", params=HOT_FUSS_PARAMS)

    assert [t["name"] for t in rendered[-1]["tracks"]] == ["Mr. Brightside"]


# --- downloaded-state matching: no second copy is offered ---


@pytest.mark.parametrize("fields", [
    {"youtube_id": "vid-bright"},
    {"spotify_uri": "spotify:track:t2"},
    {"title": "Mr Brightside", "artist": "The Killers, Someone"},
    {"album": "Hot Fuss (Deluxe Edition)", "artist": "the killers"},
], ids=["youtube_id", "spotify_uri_only", "normalised_title_artist", "album_variant"])
async def test_youtube_fallback_shows_a_library_song_as_downloaded(
        client, db, rate_limited, no_spotify, youtube, rendered, fields):
    await seed(db, **fields)

    resp = await client.get(URL, params=HOT_FUSS_PARAMS)

    assert status_of(rendered[-1], "Mr. Brightside") == "done"
    assert status_of(rendered[-1], "Jenny Was a Friend of Mine") is None
    assert resp.text.count("dl-btn") == 1


async def test_the_same_title_on_another_album_is_not_a_match(client, db, rate_limited, no_spotify, youtube, rendered):
    await seed(db, album="Live from the Royal Albert Hall")

    await client.get(URL, params=HOT_FUSS_PARAMS)

    assert status_of(rendered[-1], "Mr. Brightside") is None


@pytest.mark.parametrize("fields", [
    {"spotify_uri": "spotify:track:t2"},
    {"isrc": "USIR10400002", "title": "Mr Brightside (Remastered)"},
    {"youtube_id": "vid-other"},
], ids=["spotify_uri", "isrc", "title_artist_album"])
async def test_spotify_album_shows_a_library_song_as_downloaded(client, db, spotify_calls, rendered, fields):
    await seed(db, **fields)

    await client.get(URL)

    assert status_of(rendered[-1], "Mr. Brightside") == "done"


async def test_downloading_the_youtube_album_does_not_copy_a_song_the_library_has(
        client, db, rate_limited, no_spotify, youtube):
    existing = await seed(db, spotify_uri="spotify:track:t2")

    resp = await client.post("/api/music/download/album/youtube/MPREb_hot", data={"format": "mp3"})

    details = {d["track_id"]: d["status"] for d in resp.json()["details"]}
    assert details[existing] == "done"
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Track).where(Track.title == "Mr. Brightside")) == 1


# --- rate-limit hygiene ---


def test_no_spotify_call_while_rate_limited(rate_limited, no_spotify):
    assert spotify_albums.get_album(ALBUM_ID) is None


def test_artist_genres_are_not_fetched_while_rate_limited(rate_limited, no_spotify):
    assert spotify_client.get_artist_genres("a1") == []


def test_a_rate_limited_isrc_lookup_stops_and_starts_the_window():
    class RateLimitedTracks:
        def __init__(self):
            self.calls = 0

        def tracks(self, batch):
            self.calls += 1
            raise spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "600"})

    fake = RateLimitedTracks()

    assert spotify_client._fetch_isrcs(fake, [f"t{i}" for i in range(120)]) == {}
    assert fake.calls == 1 and spotify_client.is_rate_limited()


class RateLimitedSavedAlbums:
    def __init__(self):
        self.calls = 0

    def current_user_saved_albums(self, limit, offset):
        self.calls += 1
        raise spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "600"})


def test_saved_albums_are_not_fetched_while_rate_limited(rate_limited, no_spotify):
    assert spotify_client.get_saved_albums() is None


async def test_a_rate_limited_albums_tab_starts_the_window_and_keeps_the_youtube_albums(client, db, monkeypatch):
    fake = RateLimitedSavedAlbums()
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake)
    monkeypatch.setattr(agg.yt, "get_library_albums", lambda limit: [
        {"id": "MPREb_yt", "name": "Sam's Town", "artist": "The Killers", "image_url": None, "year": "2006"}])
    monkeypatch.setattr(agg, "_compute_entity_recency", lambda: {"albums": {}})

    resp = await client.get("/api/music/albums")

    assert resp.status_code == 200 and "Sam&#39;s Town" in resp.text
    assert fake.calls == 1 and spotify_client.is_rate_limited()


# --- the YouTube fallback keeps saying why, and matches what the library calls the album ---


async def test_the_youtube_fallback_names_the_spotify_album_it_stands_in_for(
        client, db, rate_limited, no_spotify, youtube, rendered):
    await client.get(URL, params=HOT_FUSS_PARAMS)

    assert rendered[-1]["spotify_album"] == {"id": ALBUM_ID, "name": "Hot Fuss"}


async def test_download_album_on_the_youtube_fallback_keeps_the_notice(
        client, db, rate_limited, no_spotify, youtube, rendered):
    await client.post("/api/music/download/album/youtube/MPREb_hot", headers={"HX-Request": "true"},
                      data={"format": "mp3", "spotify_album_id": ALBUM_ID, "spotify_album_name": "Hot Fuss"})

    assert rendered[-1]["notice"] == ("Spotify is rate-limiting Leasure (try again in ~2 h), "
                                      "so this album comes from YouTube Music.")
    assert rendered[-1]["spotify_album"] == {"id": ALBUM_ID, "name": "Hot Fuss"}


async def test_download_album_of_a_plain_youtube_album_has_no_notice(client, db, youtube, rendered):
    await client.post("/api/music/download/album/youtube/MPREb_hot", headers={"HX-Request": "true"},
                      data={"format": "mp3"})

    assert "notice" not in rendered[-1] and "spotify_album" not in rendered[-1]


NIGHT_AT_THE_OPERA = {
    "album": {"name": "A Night At The Opera (Remastered 2011)", "artist": "Queen", "album_artist": "Queen",
              "image_url": None, "year": "1975", "total_tracks": 1, "total_discs": None},
    "tracks": [{"id": "vid-bohemian", "name": "Bohemian Rhapsody", "artist": "Queen", "track_number": 1,
                "duration_ms": 354000, "album_artist": "Queen", "year": 1975}],
}


@pytest.fixture
def youtube_opera(monkeypatch):
    from routers import music as music_router

    result = {"browseId": "MPREb_opera", "title": NIGHT_AT_THE_OPERA["album"]["name"], "artists": [{"name": "Queen"}]}
    monkeypatch.setattr(agg.yt, "browse_client", lambda: FakeSearch(albums=[result]))
    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: json.loads(json.dumps(NIGHT_AT_THE_OPERA)))


async def seed_opera(db, album="A Night At The Opera (2011 Remaster)") -> int:
    return await seed(db, title="Bohemian Rhapsody - Remastered 2011", artist="Queen", album_artist="Queen",
                      album=album, spotify_uri="spotify:track:bohemian", isrc="GBUM71029604")


@pytest.mark.parametrize("library_album", ["A Night At The Opera (2011 Remaster)", "A Night At The Opera (Super Deluxe)"],
                         ids=["remaster_wording", "spotify_only_edition_name"])
async def test_the_youtube_fallback_matches_the_spotify_editions_wording(
        client, db, rate_limited, no_spotify, youtube_opera, rendered, library_album):
    await seed_opera(db, album=library_album)

    await client.get(URL, params={"name": library_album, "artist": "Queen"})

    assert rendered[-1]["provider"] == "youtube"
    assert status_of(rendered[-1], "Bohemian Rhapsody") == "done"


async def test_downloading_the_youtube_fallback_never_copies_a_remastered_spotify_song(
        client, db, rate_limited, no_spotify, youtube_opera):
    await seed_opera(db)

    await client.post("/api/music/download/album/youtube/MPREb_opera", data={"format": "mp3"})

    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Track)) == 1


# --- a title+artist match never overrides a different ISRC or an album-less row ---


async def test_a_different_isrc_is_a_different_recording(client, db, spotify_calls, rendered):
    await seed(db, isrc="USIR10400099")

    await client.get(URL)

    assert status_of(rendered[-1], "Mr. Brightside") is None


async def test_a_row_without_an_album_matches_no_album(client, db, spotify_calls, rendered):
    await seed(db, album=None, youtube_id="vid-search-result")

    await client.get(URL)

    assert status_of(rendered[-1], "Mr. Brightside") is None
