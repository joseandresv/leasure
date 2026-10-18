import time
from datetime import datetime

import httpx
import pytest
import spotipy

from config import settings
from models import Track
from routers import art
from services import artwork, spotify_client

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


async def _add_track(db, **fields):
    async with db() as session:
        track = Track(**{"title": "t", "artist": "A", "status": "done", **fields})
        session.add(track)
        await session.commit()
        return track


def _audio_with_sidecar(name: str) -> str:
    folder = settings.library_dir / "Artist" / "Album"
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{name}.mp3"
    audio.write_bytes(b"audio")
    (folder / f"{name}.jpg").write_bytes(JPEG)
    return str(audio)


@pytest.mark.asyncio
async def test_art_route_serves_the_sidecar_jpg(db, client):
    track = await _add_track(db, file_path=_audio_with_sidecar("01 - Mr. Brightside"))
    resp = await client.get(f"/api/library/art/{track.id}")
    assert (resp.status_code, resp.headers["content-type"], resp.content) == (200, "image/jpeg", JPEG)
    assert "max-age" in resp.headers["cache-control"]


@pytest.mark.asyncio
async def test_art_route_404s_without_a_track_or_a_sidecar(db, client):
    folder = settings.library_dir / "NoArt"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "song.mp3").write_bytes(b"audio")
    track = await _add_track(db, file_path=str(folder / "song.mp3"))
    assert (await client.get(f"/api/library/art/{track.id}")).status_code == 404
    assert (await client.get("/api/library/art/999999")).status_code == 404


@pytest.mark.asyncio
async def test_art_route_never_serves_a_file_outside_the_library(db, client, tmp_path):
    (tmp_path / "secret.jpg").write_bytes(b"secret")
    outside = await _add_track(db, file_path=str(tmp_path / "secret.mp3"))
    (settings.library_dir.parent / "secret.jpg").write_bytes(b"secret")
    traversal = await _add_track(db, title="t2", file_path=str(settings.library_dir / ".." / "secret.mp3"))
    for track in (outside, traversal):
        assert (await client.get(f"/api/library/art/{track.id}")).status_code == 404


@pytest.mark.asyncio
async def test_carousel_uses_the_sidecar_when_artwork_url_is_empty(db, client):
    track = await _add_track(db, album="Hot Fuss", file_path=_audio_with_sidecar("Somebody Told Me"))
    resp = await client.get("/api/home/carousel")
    assert f'src="/api/library/art/{track.id}"' in resp.text
    assert "lp-cover-placeholder" not in resp.text


@pytest.mark.asyncio
async def test_carousel_puts_newest_albums_with_art_first(db, client):
    await _add_track(db, album="Aaa No Art", downloaded_at=datetime(2026, 10, 1))
    await _add_track(db, album="Old", artwork_url="https://i.scdn.co/image/old", downloaded_at=datetime(2026, 1, 1))
    await _add_track(db, album="New", artwork_url="https://i.scdn.co/image/new", downloaded_at=datetime(2026, 9, 1))
    text = (await client.get("/api/home/carousel")).text
    assert text.index("image/new") < text.index("image/old") < text.index("lp-cover-placeholder")


@pytest.mark.asyncio
async def test_library_graph_node_falls_back_to_the_sidecar(db, client):
    track = await _add_track(db, album="Hot Fuss", album_artist="A", file_path=_audio_with_sidecar("Jenny"))
    nodes = (await client.get("/api/library/graph")).json()["nodes"]
    assert [n["image"] for n in nodes] == [f"/api/library/art/{track.id}"]


def _mock_upstream(monkeypatch, handler):
    monkeypatch.setattr(art, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_proxy_passes_an_allowed_image_through(client, monkeypatch):
    _mock_upstream(monkeypatch, lambda req: httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
    resp = await client.get("/api/art/proxy", params={"url": "https://www.gstatic.com/youtube/media/ytm/x.jpg"})
    assert (resp.status_code, resp.content, resp.headers["content-type"]) == (200, JPEG, "image/jpeg")
    assert "max-age" in resp.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://www.gstatic.com/x.jpg",
    "https://evil.example/x.jpg",
    "https://www.gstatic.com.evil.example/x.jpg",
    "https://user@www.gstatic.com/x.jpg",
    "https://www.gstatic.com:8443/x.jpg",
    "file:///etc/passwd",
])
async def test_proxy_rejects_urls_outside_the_allowlist(client, monkeypatch, url):
    _mock_upstream(monkeypatch, lambda req: pytest.fail("must not fetch"))
    assert (await client.get("/api/art/proxy", params={"url": url})).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<svg onload=alert(1)>", headers={"content-type": "image/svg+xml"}),
    httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
    httpx.Response(302, headers={"location": "https://evil.example/x.jpg"}),
    httpx.Response(404),
])
async def test_proxy_refuses_non_images_redirects_and_errors(client, monkeypatch, response):
    _mock_upstream(monkeypatch, lambda req: response)
    resp = await client.get("/api/art/proxy", params={"url": "https://i.ytimg.com/vi/x/hq.jpg"})
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_proxy_caps_the_image_size(client, monkeypatch):
    async def chunks():
        for _ in range(6):
            yield b"\xff" * (1024 * 1024)

    # No content-length: the cap has to hold while streaming.
    _mock_upstream(monkeypatch, lambda req: httpx.Response(200, content=chunks(), headers={"content-type": "image/png"}))
    resp = await client.get("/api/art/proxy", params={"url": "https://lh3.googleusercontent.com/a.png"})
    assert resp.status_code == 502


def test_spotify_client_does_not_retry_a_429(monkeypatch):
    monkeypatch.setattr(settings, "spotify_client_id", "cid")
    monkeypatch.setattr(settings, "spotify_client_secret", "sec")
    monkeypatch.setattr(spotify_client.SpotifyOAuth, "get_cached_token", lambda self: {"access_token": "x"})
    sp = spotify_client.get_client()
    retry = sp._session.get_adapter("https://api.spotify.com").max_retries
    assert not retry.is_retry("GET", 429, has_retry_after=True)
    assert sp.requests_timeout == spotify_client.REQUEST_TIMEOUT_S


class RateLimitedSpotify:
    def __init__(self):
        self.calls = 0

    def album(self, album_id):
        self.calls += 1
        raise spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "80000"})


def test_rate_limited_album_lookup_returns_none_at_once(monkeypatch, caplog):
    fake = RateLimitedSpotify()
    monkeypatch.setattr(spotify_client, "get_client", lambda: fake)
    monkeypatch.setattr(spotify_client, "_rate_limited_until", 0.0)
    start = time.monotonic()
    assert spotify_client.get_album_tracks("al1") is None
    assert spotify_client.get_album_tracks("al2") is None
    assert time.monotonic() - start < 1
    assert fake.calls == 1
    assert [r.message for r in caplog.records if "Retry-After 80000" in r.message]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://music.youtube.com/image/mixart?r=abc&v=1",
    "https://static.qobuz.com/images/covers/ab/cd/0886443927087_600.jpg",
])
async def test_proxy_allows_image_paths_on_page_hosts(client, monkeypatch, url):
    _mock_upstream(monkeypatch, lambda req: httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
    assert (await client.get("/api/art/proxy", params={"url": url})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://music.youtube.com/watch?v=x",
    "https://music.youtube.com/image/../watch",
    "https://static.qobuz.com/images/%2e%2e/x",
    "https://static.qobuz.com/images/%2E%2E/x",
    "https://static.qobuz.com/other",
])
async def test_proxy_rejects_other_paths_on_page_hosts(client, monkeypatch, url):
    _mock_upstream(monkeypatch, lambda req: pytest.fail("must not fetch"))
    resp = await client.get("/api/art/proxy", params={"url": url})
    assert resp.status_code == 400
    assert "Spotify, YouTube or Qobuz" in resp.json()["detail"]


def _mock_artwork_host(monkeypatch, handler) -> list[bool]:
    redirects: list[bool] = []

    def client(follow_redirects):
        redirects.append(follow_redirects)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=follow_redirects)

    monkeypatch.setattr(artwork, "_client", client)
    return redirects


@pytest.mark.asyncio
async def test_fetch_artwork_returns_the_image_bytes(monkeypatch):
    _mock_artwork_host(monkeypatch, lambda req: httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
    assert await artwork.fetch_artwork("https://i.scdn.co/image/x") == JPEG


@pytest.mark.asyncio
async def test_fetch_artwork_refuses_more_than_5_mib(monkeypatch):
    async def chunks():
        for _ in range(6):
            yield b"\xff" * (1024 * 1024)

    _mock_artwork_host(monkeypatch, lambda req: httpx.Response(200, content=chunks(), headers={"content-type": "image/png"}))
    assert await artwork.fetch_artwork("https://i.scdn.co/image/x") is None


@pytest.mark.asyncio
async def test_fetch_artwork_refuses_a_non_image(monkeypatch):
    _mock_artwork_host(monkeypatch, lambda req: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    assert await artwork.fetch_artwork("https://i.scdn.co/image/x") is None


@pytest.mark.asyncio
async def test_fetch_artwork_does_not_follow_a_qobuz_redirect(monkeypatch):
    def handler(req):
        if req.url.host == "static.qobuz.com":
            return httpx.Response(302, headers={"location": "https://evil.example/x.jpg"})
        pytest.fail("followed the redirect")

    redirects = _mock_artwork_host(monkeypatch, handler)
    assert await artwork.fetch_artwork("https://static.qobuz.com/images/covers/x_600.jpg") is None
    assert redirects == [False]


@pytest.mark.asyncio
async def test_fetch_artwork_logs_the_host_not_the_url(monkeypatch, caplog):
    _mock_artwork_host(monkeypatch, lambda req: httpx.Response(404))
    await artwork.fetch_artwork("https://i.scdn.co/image/x?sig=SECRET")
    assert "i.scdn.co" in caplog.text and "SECRET" not in caplog.text
