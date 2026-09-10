import json
from datetime import datetime

import pytest

from models import Track
from services import oauth_state


async def _add_track(db, **fields):
    async with db() as session:
        t = Track(**fields)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.mark.asyncio
async def test_home_renders(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


@pytest.mark.asyncio
async def test_status_bar_escapes_provider_metadata(client, db):
    await _add_track(db, title='<img src=x onerror="alert(1)">', artist="<b>evil</b>", status="downloading")
    resp = await client.get("/api/status/bar")
    assert resp.status_code == 200
    assert "<img src=x" not in resp.text
    assert "&lt;b&gt;evil&lt;/b&gt;" in resp.text


@pytest.mark.asyncio
async def test_carousel_escapes_and_drops_non_http_urls(client, db):
    await _add_track(db, title="t", artist='a"><script>', album='al"', status="done",
                     artwork_url='javascript:alert(1)')
    await _add_track(db, title="t2", artist="a2", album='x" onload="alert(1)', status="done",
                     artwork_url="https://i.scdn.co/image/abc")
    resp = await client.get("/api/home/carousel")
    assert "javascript:" not in resp.text
    assert '<script>' not in resp.text
    assert 'onload="alert' not in resp.text
    assert "https://i.scdn.co/image/abc" in resp.text


@pytest.mark.asyncio
async def test_sync_start_rejects_undetected_path(client, tmp_path):
    resp = await client.post("/api/device/sync/start", data={"device_path": str(tmp_path), "scope": "all"})
    assert resp.status_code == 400
    assert "not a detected removable drive" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_cross_site_post_is_rejected(client, fake_device):
    resp = await client.post("/api/device/sync/start", data={"device_path": str(fake_device)},
                             headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    resp = await client.post("/api/device/sync/start", data={"device_path": str(fake_device)},
                             headers={"Origin": "http://evil.example", "Host": "127.0.0.1:8642"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sync_stream_unknown_job_is_404(client):
    resp = await client.get("/api/device/sync/stream/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sync_get_stream_without_job_is_gone(client, fake_device):
    # The old CSRF-able GET form must not exist any more
    resp = await client.get("/api/device/sync/stream", params={"device_path": str(fake_device)})
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_full_sync_job_copies_files_and_writes_manifest(client, db, fake_device, tmp_path):
    from services.playlist import MANIFEST_NAME

    lib = tmp_path / "lib" / "Artist" / "Album"
    lib.mkdir(parents=True)
    src = lib / "01 - Song.mp3"
    src.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 413)
    (lib / "01 - Song.jpg").write_bytes(b"jpg")
    t = await _add_track(db, title="Song", artist="Artist", album="Album", track_number=1,
                         status="done", file_path=str(src), format="mp3")

    from models import Playlist, PlaylistTrack
    async with db() as session:
        pl = Playlist(name="Mix", source="spotify", source_id="x")
        session.add(pl)
        await session.commit()
        await session.refresh(pl)
        session.add(PlaylistTrack(playlist_id=pl.id, track_id=t.id, position=0))
        await session.commit()

    (fake_device / "User Made.m3u").write_text("keep me")

    start = await client.post("/api/device/sync/start", data={"device_path": str(fake_device), "scope": "all"})
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]

    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{job_id}") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "done"
    done = events[-1]
    assert done["synced"] == 1 and done["errors"] == [] and done["playlists"] == 1

    assert (fake_device / "Artist" / "Album" / "01 - Song.mp3").exists()
    assert (fake_device / "Artist" / "Album" / "01 - Song.jpg").exists()
    assert (fake_device / "Mix.m3u8").exists()
    assert (fake_device / "User Made.m3u").exists()
    manifest = json.loads((fake_device / MANIFEST_NAME).read_text())
    assert manifest["files"] == ["Mix.m3u8"]

    # A job id is single-use
    again = await client.get(f"/api/device/sync/stream/{job_id}")
    assert again.status_code == 404

    async with db() as session:
        refreshed = await session.get(Track, t.id)
        assert isinstance(refreshed.synced_at, datetime)

    # Diff now sees the track as already synced, not as still to add (POSIX path comparison)
    diff = await client.get("/api/device/diff/html", params={"device_path": str(fake_device)})
    assert diff.status_code == 200
    assert "1 already on device" in diff.text
    assert "tracks to add" not in diff.text


@pytest.mark.asyncio
async def test_sync_survives_a_client_that_stops_streaming(client, db, fake_device, tmp_path):
    import asyncio

    from sqlalchemy import select

    from models import SyncHistory

    tracks = []
    for i in range(1, 4):
        lib = tmp_path / "lib" / "Artist" / "Album"
        lib.mkdir(parents=True, exist_ok=True)
        src = lib / f"0{i} - Song {i}.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 413)
        tracks.append(await _add_track(db, title=f"Song {i}", artist="Artist", album="Album",
                                       track_number=i, status="done", file_path=str(src), format="mp3"))

    start = await client.post("/api/device/sync/start",
                              data={"device_path": str(fake_device), "scope": "all"})
    job_id = start.json()["job_id"]

    # Read one event, then walk away the way a closed tab does
    from routers.device import _SYNC_JOBS, sync_stream
    stream = await sync_stream(job_id)
    first = await stream.body_iterator.__anext__()
    assert '"start"' in first
    await stream.body_iterator.aclose()

    task = _SYNC_JOBS[job_id]["task"]
    for _ in range(200):
        if task.done():
            break
        await asyncio.sleep(0.02)
    assert task.done(), "sync task did not finish after the client left"

    for i in range(1, 4):
        assert (fake_device / "Artist" / "Album" / f"0{i} - Song {i}.mp3").exists()
    async with db() as session:
        for t in tracks:
            assert isinstance((await session.get(Track, t.id)).synced_at, datetime)
        history = (await session.execute(select(SyncHistory))).scalars().all()
        assert len(history) == 1 and history[0].tracks_added == 3


@pytest.mark.asyncio
async def test_read_only_device_views_reject_undetected_path(client, tmp_path):
    diff = await client.get("/api/device/diff/html", params={"device_path": str(tmp_path)})
    assert diff.status_code == 200 and "not found" in diff.text
    info = await client.get("/api/device/info", params={"device_path": str(tmp_path)})
    assert "error" in info.json()
    files = await client.get("/api/device/files/html", params={"device_path": str(tmp_path)})
    assert files.status_code == 200 and "not found" in files.text


@pytest.mark.asyncio
async def test_spotify_callback_requires_valid_state(client):
    resp = await client.get("/api/spotify/callback", params={"code": "abc", "state": "forged"})
    assert resp.status_code == 400
    resp = await client.get("/api/spotify/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "spotify_error=access_denied" in resp.headers["location"]


@pytest.mark.asyncio
async def test_youtube_callback_requires_valid_state(client):
    resp = await client.get("/api/youtube/oauth/callback", params={"code": "abc", "state": "forged"})
    assert resp.status_code == 400


def test_oauth_state_is_single_use_and_rejects_unknown():
    token = oauth_state.issue()
    assert oauth_state.verify(token)
    assert not oauth_state.verify(token)
    assert not oauth_state.verify("nope")
    assert not oauth_state.verify(None)


@pytest.mark.asyncio
async def test_spotify_status_html_links_to_auth_route(client, monkeypatch):
    import config
    from services import spotify_client

    monkeypatch.setattr(config.settings, "spotify_client_id", "cid")
    monkeypatch.setattr(config.settings, "spotify_client_secret", "sec")
    monkeypatch.setattr(spotify_client, "is_connected", lambda: False)

    resp = await client.get("/api/spotify/status/html")
    assert resp.status_code == 200
    assert 'href="/api/spotify/auth"' in resp.text
    assert "accounts.spotify.com" not in resp.text


@pytest.mark.asyncio
async def test_carousel_short_strip_is_not_duplicated(client, db):
    art = "https://i.scdn.co/image/one-album"
    for n in range(1, 4):
        await _add_track(db, title=f"t{n}", artist="A", album="Album", status="done", artwork_url=art)
    resp = await client.get("/api/home/carousel")
    assert resp.status_code == 200
    assert resp.text.count(art) == 1
    assert "3 tracks across 1 album<" in resp.text


@pytest.mark.asyncio
async def test_mount_sudo_hint_is_neutral_and_selectable(client, monkeypatch):
    import subprocess

    from routers import device as device_router
    from services import platform as platform_service

    monkeypatch.setattr(platform_service, "get_platform", lambda: "wsl2")
    monkeypatch.setattr(device_router, "get_platform", lambda: "wsl2")
    monkeypatch.setattr(
        device_router.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", "sudo: a password is required"),
    )

    resp = await client.post("/api/device/mount", data={"letter": "e"})
    assert resp.status_code == 200
    assert "sudo mount -t drvfs E: /mnt/e" in resp.text
    assert "--color-error" not in resp.text
    assert "user-select" in resp.text


@pytest.mark.asyncio
async def test_spotify_status_html_hides_env_vars_behind_details(client, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "spotify_client_id", "")

    resp = await client.get("/api/spotify/status/html")
    assert resp.status_code == 200
    assert "isn't set up yet" in resp.text
    assert "SPOTIFY_CLIENT_ID" not in resp.text.split("<details")[0]


@pytest.mark.asyncio
async def test_youtube_oauth_status_html_hides_env_vars_behind_details(client, monkeypatch):
    import config
    from services import youtube_client

    monkeypatch.setattr(config.settings, "google_client_id", "")
    monkeypatch.setattr(youtube_client, "is_youtube_oauth_connected", lambda: False)

    resp = await client.get("/api/youtube/oauth/status/html")
    assert resp.status_code == 200
    assert "isn't set up yet" in resp.text
    assert "GOOGLE_CLIENT_ID" not in resp.text.split("<details")[0]


@pytest.mark.asyncio
async def test_carousel_counts_albums_that_have_no_artwork(client, db):
    await _add_track(db, title="a", artist="A", album="With Art", status="done",
                     artwork_url="https://i.scdn.co/image/one")
    await _add_track(db, title="b", artist="A", album="No Art", status="done")
    resp = await client.get("/api/home/carousel")
    assert "2 tracks across 2 albums<" in resp.text


@pytest.mark.asyncio
async def test_spotify_browse_calls_the_provider_off_the_event_loop(client, monkeypatch):
    import threading

    from routers import spotify as spotify_router

    loop_thread = threading.get_ident()
    called_on = {}

    def fake_saved_albums(limit=20, offset=0):
        called_on["thread"] = threading.get_ident()
        return {"albums": [], "total": 0, "offset": offset, "limit": limit}

    monkeypatch.setattr(spotify_router.sp, "get_saved_albums", fake_saved_albums)
    resp = await client.get("/api/spotify/albums/html")
    assert resp.status_code == 200
    assert called_on["thread"] != loop_thread


@pytest.mark.asyncio
async def test_youtube_browse_calls_the_provider_off_the_event_loop(client, monkeypatch):
    import threading

    from routers import youtube as youtube_router

    loop_thread = threading.get_ident()
    called_on = {}

    def fake_library_albums():
        called_on["thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(youtube_router.yt, "get_library_albums", fake_library_albums)
    resp = await client.get("/api/youtube/albums/html")
    assert resp.status_code == 200
    assert called_on["thread"] != loop_thread
