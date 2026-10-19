import inspect
import time

import pytest
import requests
import spotipy

from config import settings
from services import music_aggregator as agg
from services import search, spotify_client

ALBUM_ID = "4OHNH3sDzIxnmUADXzv2kT"

# Public functions that never reach the Web API (OAuth, token cache, pure helpers).
NOT_WEB_API = {"get_auth_url", "handle_callback", "get_client", "is_connected", "rate_limit_seconds_left",
               "is_rate_limited", "wait_text", "rate_limit_message", "release_year", "album_fields", "calls_today",
               "counting_as_sync"}


class RecordingClient:
    """Stands in for spotipy.Spotify and records every method called on it."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return {}
        return call


def rate_limited_429(*args, **kwargs):
    raise spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "7200"})


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(spotify_client, "_rate_limited_until", 0.0)
    monkeypatch.setattr(agg, "_albums_cache", {"data": None, "spotify": None})
    monkeypatch.setattr(agg, "_recent_cache", {"ts": 0.0, "limit": 0, "data": None})
    monkeypatch.setattr(agg, "_recency_cache", {"ts": 0.0, "data": None})


@pytest.fixture
def rate_limited(monkeypatch):
    monkeypatch.setattr(spotify_client, "_rate_limited_until", time.monotonic() + 7200)


@pytest.fixture
def recording_client(monkeypatch):
    fake = RecordingClient()
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake.calls.append("get_client") or fake)
    return fake


@pytest.fixture
def no_youtube(monkeypatch):
    class NoResults:
        def search(self, *args, **kwargs):
            return []

    async def no_musicbrainz(*args, **kwargs):
        return None

    monkeypatch.setattr(agg.yt, "get_history", lambda limit: [])
    monkeypatch.setattr(agg.yt, "get_youtube_history", lambda limit: [])
    monkeypatch.setattr(agg.yt, "get_library_albums", lambda limit: [])
    monkeypatch.setattr(search.yt, "get_client", lambda: NoResults())
    monkeypatch.setattr(search.mb, "get", no_musicbrainz)


def test_no_public_function_reaches_spotify_while_rate_limited(rate_limited, recording_client, monkeypatch):
    monkeypatch.setattr(spotify_client, "_get_auth_manager", lambda: recording_client)
    web_api = {name: fn for name, fn in inspect.getmembers(spotify_client, inspect.isfunction)
               if fn.__module__ == spotify_client.__name__ and not name.startswith("_") and name not in NOT_WEB_API}

    results = {}
    for name, fn in web_api.items():
        required = [p for p in inspect.signature(fn).parameters.values() if p.default is inspect.Parameter.empty]
        results[name] = fn(*[ALBUM_ID] * len(required))

    assert recording_client.calls == [] and all(r in (None, []) for r in results.values())
    assert {"get_user_profile", "get_currently_playing", "get_recently_played", "get_playlists",
            "get_liked_songs", "search_catalog"} <= web_api.keys()


def _guarded_client(monkeypatch, response_status: int = 200):
    monkeypatch.setattr(settings, "spotify_client_id", "cid")
    monkeypatch.setattr(settings, "spotify_client_secret", "sec")
    monkeypatch.setattr(spotify_client.SpotifyOAuth, "get_cached_token", lambda self: {"access_token": "x"})
    client = spotify_client.get_client()
    sent: list[str] = []

    def request(method, url, **kwargs):
        sent.append(url)
        resp = requests.Response()
        resp.status_code, resp.url, resp._content = response_status, url, b"{}"
        resp.headers["Retry-After"] = "7200"
        return resp

    monkeypatch.setattr(client, "_auth_headers", lambda: {})
    monkeypatch.setattr(client._session, "request", request)
    return client, sent


def test_a_direct_client_call_is_not_sent_while_rate_limited(rate_limited, monkeypatch):
    client, sent = _guarded_client(monkeypatch)

    with pytest.raises(spotipy.SpotifyException) as raised:
        client.search(q="hot fuss")

    assert raised.value.http_status == 429 and sent == []


def test_a_429_from_any_client_call_starts_the_window(monkeypatch):
    client, sent = _guarded_client(monkeypatch, response_status=429)

    with pytest.raises(spotipy.SpotifyException):
        client.current_user()

    assert len(sent) == 1 and spotify_client.rate_limit_seconds_left() > 7000


async def test_status_card_stays_connected_and_says_spotify_is_rate_limiting(client, monkeypatch):
    fake = RecordingClient()
    fake.current_user = rate_limited_429
    monkeypatch.setattr(settings, "spotify_client_id", "cid")
    monkeypatch.setattr(spotify_client, "is_connected", lambda: True)
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake)

    resp = await client.get("/api/spotify/status/html")

    assert resp.status_code == 200
    assert "Connected" in resp.text and "Spotify is rate-limiting Leasure (try again in ~2 h)" in resp.text


async def test_search_skips_spotify_and_says_why_while_rate_limited(client, rate_limited, recording_client, no_youtube):
    resp = await client.get("/api/music/search", params={"q": "hot fuss"})

    assert recording_client.calls == []
    assert "Spotify is rate-limiting Leasure (try again in ~2 h), so Spotify was not searched." in resp.text


async def test_deck_skips_spotify_while_rate_limited(client, rate_limited, recording_client, no_youtube):
    resp = await client.get("/api/music/deck")

    assert resp.status_code == 200 and recording_client.calls == []


def test_rate_limited_albums_keep_the_last_spotify_albums(monkeypatch):
    fake = RecordingClient()
    fake.current_user_saved_albums = lambda limit, offset: {"total": 1, "items": [{"album": {
        "id": ALBUM_ID, "name": "Hot Fuss", "artists": [{"name": "The Killers"}], "images": [],
        "total_tracks": 11, "release_date": "2004-06-07", "uri": f"spotify:album:{ALBUM_ID}"}}]}
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake)
    monkeypatch.setattr(agg.yt, "get_library_albums", lambda limit: [
        {"id": "MPREb_hot", "name": "Hot Fuss", "artist": "The Killers", "image_url": None, "year": "2004"},
        {"id": "MPREb_sam", "name": "Sam's Town", "artist": "The Killers", "image_url": None, "year": "2006"}])
    monkeypatch.setattr(agg, "_compute_entity_recency", lambda: {"albums": {}})
    first = agg.get_unified_albums()

    fake.current_user_saved_albums = rate_limited_429
    again = agg.get_unified_albums()

    assert spotify_client.is_rate_limited() and again == first


async def test_empty_albums_tab_says_spotify_is_rate_limiting(client, db, rate_limited, recording_client, no_youtube,
                                                             monkeypatch):
    monkeypatch.setattr(agg, "_compute_entity_recency", lambda: {"albums": {}})

    resp = await client.get("/api/music/albums")

    assert "Spotify is rate-limiting Leasure (try again in ~2 h); YouTube Music albums still show here." in resp.text


@pytest.mark.parametrize("tab", ["deck", "recent", "playlists", "artists"])
async def test_empty_tabs_get_the_rate_limit_notice(client, rate_limited, recording_client, no_youtube, monkeypatch, tab):
    from routers import music

    contexts = []
    render = music.templates.TemplateResponse
    monkeypatch.setattr(music.templates, "TemplateResponse",
                        lambda **kwargs: contexts.append(kwargs["context"]) or render(**kwargs))
    monkeypatch.setattr(agg.yt, "get_playlists", lambda: [])
    monkeypatch.setattr(agg, "_compute_entity_recency", lambda: {"albums": {}, "artists": {}, "playlists": {},
                                                                 "_yt_recent_tracks": []})

    resp = await client.get(f"/api/music/{tab}")

    assert resp.status_code == 200 and recording_client.calls == []
    assert contexts[0]["spotify_notice"] == "Spotify is rate-limiting Leasure (try again in ~2 h)"
    assert "Spotify is rate-limiting Leasure (try again in ~2 h); YouTube Music" in resp.text
