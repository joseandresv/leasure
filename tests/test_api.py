import json
import re
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


@pytest.mark.asyncio
async def test_download_track_stores_the_isrc(client, db):
    from sqlalchemy import select

    resp = await client.post("/api/music/download/track", data={
        "title": "Blue Monday", "artist": "New Order", "album": "Substance",
        "spotify_uri": "spotify:track:isrctest", "isrc": "GBAAA8700001", "format": "mp3"})
    assert resp.status_code == 200

    async with db() as session:
        track = (await session.execute(
            select(Track).where(Track.spotify_uri == "spotify:track:isrctest"))).scalar_one()
    assert track.isrc == "GBAAA8700001"


@pytest.mark.asyncio
async def test_library_tracks_html_marks_a_premium_download(client, db):
    await _add_track(db, title="Blue Monday", artist="New Order", status="done",
                     format="m4a", quality="aac_256", premium_used=True, source_bitrate_kbps=256)

    resp = await client.get("/api/library/tracks/html")
    assert "Premium" in resp.text


def _hx_vals(html: str) -> list[dict]:
    return [json.loads(m) for m in re.findall(r"hx-vals='([^']*)'", html)]


@pytest.mark.asyncio
async def test_playlist_track_download_keeps_its_own_album(client, db, monkeypatch):
    from routers import music as music_router

    monkeypatch.setattr(music_router.yt, "get_playlist_tracks", lambda pid, limit=500: {
        "playlist": {"name": "Road Trip", "image_url": "https://img/playlist.jpg"},
        "tracks": [{"id": "vid1", "name": "Blue Monday", "artist": "New Order",
                    "album": "Substance", "album_artist": "New Order", "duration_ms": 1000,
                    "image_url": "https://img/substance.jpg"}],
    })

    resp = await client.get("/api/music/playlist/youtube/PL1")
    assert resp.status_code == 200
    vals = _hx_vals(resp.text)[0]
    assert vals["album"] == "Substance"
    assert vals["album_artist"] == "New Order"
    assert vals["image_url"] == "https://img/substance.jpg"


@pytest.mark.asyncio
async def test_playlist_track_without_an_album_falls_back_to_the_playlist(client, db, monkeypatch):
    from routers import music as music_router

    monkeypatch.setattr(music_router.yt, "get_playlist_tracks", lambda pid, limit=500: {
        "playlist": {"name": "Road Trip", "image_url": "https://img/playlist.jpg"},
        "tracks": [{"id": "vid2", "name": "Untitled", "artist": "Nobody",
                    "album": None, "album_artist": None, "duration_ms": 1000,
                    "image_url": None}],
    })

    resp = await client.get("/api/music/playlist/youtube/PL1")
    vals = _hx_vals(resp.text)[0]
    assert vals["album"] == "Road Trip"
    assert vals["album_artist"] == ""
    assert vals["image_url"] == "https://img/playlist.jpg"


@pytest.mark.asyncio
async def test_spotify_playlist_track_download_uses_its_album_cover(client, db, monkeypatch):
    from routers import music as music_router

    monkeypatch.setattr(music_router.sp, "get_playlist_tracks", lambda pid: {
        "playlist": {"name": "Road Trip", "image_url": "https://img/playlist.jpg"},
        "tracks": [{"id": "t1", "uri": "spotify:track:t1", "name": "Blue Monday",
                    "artist": "New Order", "album": "Substance", "album_artist": "New Order",
                    "album_image_url": "https://img/substance.jpg", "duration_ms": 1000}],
    })

    resp = await client.get("/api/music/playlist/spotify/PL1")
    vals = _hx_vals(resp.text)[0]
    assert vals["image_url"] == "https://img/substance.jpg"


@pytest.mark.asyncio
async def test_bulk_playlist_download_stores_each_track_own_album_cover(client, db, monkeypatch):
    from sqlalchemy import select

    from routers import music as music_router

    monkeypatch.setattr(music_router.sp, "get_playlist_tracks", lambda pid: {
        "playlist": {"name": "Road Trip", "image_url": "https://img/playlist.jpg"},
        "tracks": [{"uri": "spotify:track:t1", "name": "Blue Monday", "artist": "New Order",
                    "album": "Substance", "album_artist": "New Order",
                    "album_image_url": "https://img/substance.jpg", "duration_ms": 1000}],
    })

    resp = await client.post("/api/music/download/playlist/spotify/PL1", data={"format": "mp3"})
    assert resp.status_code == 200

    async with db() as session:
        track = (await session.execute(
            select(Track).where(Track.spotify_uri == "spotify:track:t1"))).scalar_one()
    assert track.artwork_url == "https://img/substance.jpg"


@pytest.mark.asyncio
async def test_album_track_download_still_uses_the_album_name(client, db, monkeypatch):
    from routers import music as music_router

    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: {
        "album": {"name": "Substance", "artist": "New Order", "album_artist": "New Order",
                  "image_url": "https://img/substance.jpg", "year": 1987,
                  "total_tracks": 1, "total_discs": None},
        "tracks": [{"id": "vid3", "name": "Blue Monday", "artist": "New Order",
                    "track_number": 1, "duration_ms": 1000, "album_artist": "New Order",
                    "year": 1987}],
    })

    resp = await client.get("/api/music/album/youtube/MPREb1")
    vals = _hx_vals(resp.text)[0]
    assert vals["album"] == "Substance"
    assert vals["album_artist"] == "New Order"
    assert vals["image_url"] == "https://img/substance.jpg"


async def _run_sync_job(client, device_path) -> list[dict]:
    start = await client.post("/api/device/sync/start", data={"device_path": str(device_path), "scope": "all"})
    assert start.status_code == 200, start.text
    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


@pytest.mark.asyncio
async def test_sync_copies_a_two_disc_album_in_disc_and_track_order(client, db, fake_device, tmp_path, monkeypatch):
    import shutil

    lib = tmp_path / "lib"
    lib.mkdir()
    for disc, number in [(2, 2), (1, 3), (2, 1), (1, 1), (1, 2)]:
        src = lib / f"{disc}-{number}.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 413)
        await _add_track(db, title=f"Song {disc}{number}", artist="Daft Punk", album="RAM", album_artist="Daft Punk",
                         track_number=number, disc_number=disc, total_discs=2, status="done",
                         file_path=str(src), format="mp3")
    copied = []
    real_copyfile = shutil.copyfile

    def recording_copyfile(src, dst):
        copied.append(dst)
        return real_copyfile(src, dst)

    monkeypatch.setattr(shutil, "copyfile", recording_copyfile)

    events = await _run_sync_job(client, fake_device)

    assert events[-1]["synced"] == 5
    assert [p.rsplit("/", 1)[1].removesuffix(".leasure-part") for p in copied] == [
        "1-01 - Song 11.mp3", "1-02 - Song 12.mp3", "1-03 - Song 13.mp3", "2-01 - Song 21.mp3", "2-02 - Song 22.mp3",
    ]


async def _seed_done_track_with_sidecars(db, folder, title, **fields):
    from datetime import UTC

    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{title}.m4a"
    audio.write_bytes(b"audio")
    audio.with_suffix(".jpg").write_bytes(b"jpg")
    audio.with_suffix(".lrc").write_text("[00:01]la")
    return await _add_track(db, title=title, status="done", file_path=str(audio), format="m4a",
                            synced_at=datetime.now(UTC).replace(tzinfo=None), **fields)


@pytest.fixture
def retag_calls(monkeypatch, tmp_path, isolated_paths):
    import config
    from services import tagger

    monkeypatch.setattr(config.settings, "library_dir", tmp_path / "library")
    isolated_paths()
    calls = []

    async def fake_apply(path, track_id):
        calls.append((track_id, path))

    monkeypatch.setattr(tagger, "apply_full_metadata", fake_apply)
    return calls


@pytest.mark.asyncio
async def test_album_download_numbers_a_track_an_earlier_playlist_download_left_unnumbered(
        client, db, monkeypatch, tmp_path, retag_calls):
    from pathlib import Path

    from routers import music as music_router

    album_dir = tmp_path / "library" / "The Killers" / "Hot Fuss"
    track = await _seed_done_track_with_sidecars(db, album_dir, "Mr. Brightside", youtube_id="vid2",
                                              artist="The Killers", album="Hot Fuss", album_artist="The Killers")
    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: {
        "album": {"name": "Hot Fuss", "artist": "The Killers", "album_artist": "The Killers",
                  "image_url": None, "year": 2004, "total_tracks": 11, "total_discs": None},
        "tracks": [{"id": "vid2", "name": "Mr. Brightside", "artist": "The Killers", "track_number": 2,
                    "duration_ms": 1000, "album_artist": "The Killers", "year": 2004}],
    })

    resp = await client.post("/api/music/download/album/youtube/MPREb1", data={"format": "mp3"})
    assert resp.status_code == 200

    async with db() as session:
        refreshed = await session.get(Track, track.id)
    moved = album_dir / "02 - Mr. Brightside.m4a"
    assert (refreshed.track_number, refreshed.total_tracks, refreshed.synced_at) == (2, 11, None)
    assert Path(refreshed.file_path) == moved and moved.exists()
    assert (album_dir / "02 - Mr. Brightside.jpg").exists() and (album_dir / "02 - Mr. Brightside.lrc").exists()
    assert not (album_dir / "Mr. Brightside.m4a").exists()
    assert retag_calls == [(track.id, moved)]


@pytest.mark.asyncio
async def test_spotify_album_download_moves_a_second_disc_track_under_its_disc_prefix(
        client, db, monkeypatch, tmp_path, retag_calls):
    from routers import spotify as spotify_router

    album_dir = tmp_path / "library" / "Daft Punk" / "RAM"
    track = await _seed_done_track_with_sidecars(db, album_dir, "Contact", spotify_uri="spotify:track:c",
                                              artist="Daft Punk", album="RAM", album_artist="Daft Punk")
    monkeypatch.setattr(spotify_router.sp, "get_artist_genres", lambda artist_id: [])
    monkeypatch.setattr(spotify_router.sp, "get_album_tracks", lambda album_id: {
        "album": {"name": "RAM", "artist": "Daft Punk", "album_artist": "Daft Punk", "image_url": None,
                  "release_date": "2013-05-17", "genres": [], "total_tracks": 13, "total_discs": 2},
        "tracks": [{"uri": "spotify:track:c", "name": "Contact", "artist": "Daft Punk", "artist_id": "dp",
                    "track_number": 1, "disc_number": 2, "duration_ms": 1000}],
    })

    resp = await client.post("/api/spotify/download/album/ram")
    assert resp.status_code == 200

    async with db() as session:
        refreshed = await session.get(Track, track.id)
    assert refreshed.file_path == str(album_dir / "2-01 - Contact.m4a")
    assert (album_dir / "2-01 - Contact.m4a").exists()
    assert refreshed.synced_at is None


@pytest.mark.asyncio
async def test_album_download_leaves_an_already_numbered_track_alone(client, db, monkeypatch, tmp_path, retag_calls):
    from routers import music as music_router

    album_dir = tmp_path / "library" / "New Order" / "Substance"
    track = await _seed_done_track_with_sidecars(db, album_dir, "Blue Monday", youtube_id="vid3", artist="New Order",
                                              album="Substance", album_artist="New Order", track_number=3,
                                              total_tracks=1)
    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: {
        "album": {"name": "Substance", "artist": "New Order", "album_artist": "New Order",
                  "image_url": None, "year": 1987, "total_tracks": 1, "total_discs": None},
        "tracks": [{"id": "vid3", "name": "Blue Monday", "artist": "New Order", "track_number": 3,
                    "duration_ms": 1000, "album_artist": "New Order", "year": 1987}],
    })

    await client.post("/api/music/download/album/youtube/MPREb1", data={"format": "mp3"})

    async with db() as session:
        refreshed = await session.get(Track, track.id)
    assert refreshed.synced_at is not None
    assert retag_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "stored"), [("2", 2), ("", None)])
async def test_download_track_stores_total_discs(client, db, sent, stored):
    from sqlalchemy import select

    uri = f"spotify:track:discs{sent or 'none'}"
    resp = await client.post("/api/music/download/track", data={
        "title": "Contact", "artist": "Daft Punk", "spotify_uri": uri, "total_discs": sent, "format": "mp3"})
    assert resp.status_code == 200

    async with db() as session:
        track = (await session.execute(select(Track).where(Track.spotify_uri == uri))).scalar_one()
    assert track.total_discs == stored


@pytest.mark.asyncio
async def test_youtube_album_playlist_download_stores_the_track_numbers(client, db, monkeypatch):
    from sqlalchemy import select

    from routers import youtube as youtube_router

    monkeypatch.setattr(youtube_router.yt, "get_playlist_tracks", lambda pid: {
        "playlist": {"name": "Hot Fuss"},
        "tracks": [{"id": "olak1", "name": "Jenny", "artist": "The Killers", "album": "Hot Fuss",
                    "track_number": 1, "total_tracks": 11}],
    })

    resp = await client.post("/api/youtube/download/playlist/OLAK5uy_x")
    assert resp.status_code == 200

    async with db() as session:
        track = (await session.execute(select(Track).where(Track.youtube_id == "olak1"))).scalar_one()
    assert (track.track_number, track.total_tracks) == (1, 11)


@pytest.mark.asyncio
async def test_sync_start_is_refused_while_the_card_is_being_ejected(client, fake_device, monkeypatch):
    from routers import device as device_router

    monkeypatch.setattr(device_router, "_EJECTING", {str(fake_device)})

    resp = await client.post("/api/device/sync/start", data={"device_path": str(fake_device), "scope": "all"})

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_the_legacy_sync_counts_as_a_running_sync_for_eject(client, db, fake_device, tmp_path, monkeypatch):
    from routers import device as device_router

    src = tmp_path / "Song.mp3"
    src.write_bytes(b"audio")
    await _add_track(db, title="Song", artist="A", album="B", status="done", file_path=str(src), format="mp3")
    seen = []

    def copy_while_checking(src, target, track):
        seen.append(device_router._sync_running_to(str(fake_device)))
        return [device_router._Written("audio", "A/B/Song.mp3", src, True)]

    monkeypatch.setattr(device_router, "_copy_track", copy_while_checking)

    resp = await client.post("/api/device/sync", data={"device_path": str(fake_device), "scope": "all"})

    assert resp.status_code == 200
    assert seen == [True]
    assert not device_router._sync_running_to(str(fake_device))


@pytest.mark.asyncio
async def test_an_album_refresh_during_a_sync_is_not_marked_synced_by_it(client, db, fake_device, tmp_path, monkeypatch):
    import asyncio

    from routers import device as device_router

    loop = asyncio.get_running_loop()
    srcs = [tmp_path / f"{name}.m4a" for name in ("Refreshed", "Untouched")]
    for src in srcs:
        src.write_bytes(b"audio")
    refreshed = await _add_track(db, title="Refreshed", artist="A", album="B", status="done",
                                 file_path=str(srcs[0]), format="m4a")
    untouched = await _add_track(db, title="Untouched", artist="A", album="B", status="done",
                                 file_path=str(srcs[1]), format="m4a")

    async def album_download_renumbers():
        async with db() as session:
            row = await session.get(Track, refreshed.id)
            row.track_number, row.file_path, row.synced_at = 3, str(tmp_path / "03 - Refreshed.m4a"), None
            await session.commit()

    def copy_then_refresh(src, target, track):
        if track.id == refreshed.id:
            asyncio.run_coroutine_threadsafe(album_download_renumbers(), loop).result(5)
        return [device_router._Written("audio", f"A/B/{src.name}", src, True)]

    monkeypatch.setattr(device_router, "_copy_track", copy_then_refresh)

    events = await _run_sync_job(client, fake_device)

    assert events[-1]["synced"] == 2
    async with db() as session:
        assert (await session.get(Track, refreshed.id)).synced_at is None
        assert (await session.get(Track, untouched.id)).synced_at is not None


def test_a_failed_sidecar_move_puts_the_audio_back(tmp_path, monkeypatch):
    import errno
    import os

    from services import tracks

    folder = tmp_path / "A" / "Alb"
    folder.mkdir(parents=True)
    (folder / "Song.m4a").write_bytes(b"audio")
    (folder / "Song.jpg").write_bytes(b"jpg")
    real_replace = os.replace
    calls = []

    def replace_failing_second(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(errno.EIO, "Input/output error")
        return real_replace(src, dst)

    monkeypatch.setattr(tracks.os, "replace", replace_failing_second)

    with pytest.raises(OSError):
        tracks.relocate_library_file(folder / "Song.m4a", folder / "03 - Song")

    assert sorted(p.name for p in folder.iterdir()) == ["Song.jpg", "Song.m4a"]


def test_a_retry_adopts_a_file_an_earlier_move_left_at_the_new_name(tmp_path):
    from services import tracks

    moved = tmp_path / "03 - Song.m4a"
    moved.write_bytes(b"audio")

    assert tracks.relocate_library_file(tmp_path / "Song.m4a", tmp_path / "03 - Song") == moved
    assert tracks.relocate_library_file(tmp_path / "Song.m4a", tmp_path / "03 - Song", frozenset({str(moved)})) is None


@pytest.mark.asyncio
async def test_a_failed_commit_after_an_album_refresh_moves_the_file_back(db, tmp_path, retag_calls):
    from services.tracks import refresh_album_track

    album_dir = tmp_path / "library" / "The Killers" / "Hot Fuss"
    track = await _seed_done_track_with_sidecars(db, album_dir, "Mr. Brightside", artist="The Killers",
                                              album="Hot Fuss", album_artist="The Killers")

    async with db() as session:
        row = await session.get(Track, track.id)

        async def locked():
            raise RuntimeError("database is locked")

        session.commit = locked
        with pytest.raises(RuntimeError):
            await refresh_album_track(session, row, {"track_number": 2})

    assert sorted(p.name for p in album_dir.iterdir()) == ["Mr. Brightside.jpg", "Mr. Brightside.lrc",
                                                            "Mr. Brightside.m4a"]


@pytest.mark.asyncio
async def test_an_album_refresh_waiting_on_an_upgrade_moves_the_new_flac(db, tmp_path, retag_calls):
    import asyncio

    from services import track_locks
    from services.tracks import refresh_album_track

    album_dir = tmp_path / "library" / "The Killers" / "Hot Fuss"
    track = await _seed_done_track_with_sidecars(db, album_dir, "Mr. Brightside", artist="The Killers",
                                                 album="Hot Fuss", album_artist="The Killers")
    async with db() as session:
        row = await session.get(Track, track.id)
        async with track_locks.hold(track.id):
            refresh = asyncio.create_task(refresh_album_track(session, row, {"track_number": 5}))
            await asyncio.sleep(0)
            (album_dir / "Mr. Brightside.m4a").unlink()
            (album_dir / "Mr. Brightside.flac").write_bytes(b"flac")
            async with db() as other:
                (await other.get(Track, track.id)).file_path = str(album_dir / "Mr. Brightside.flac")
                await other.commit()
        await refresh

    async with db() as session:
        stored = await session.get(Track, track.id)
    assert stored.file_path == str(album_dir / "05 - Mr. Brightside.flac")
    assert (album_dir / "05 - Mr. Brightside.flac").read_bytes() == b"flac"
    assert retag_calls == [(track.id, album_dir / "05 - Mr. Brightside.flac")]


@pytest.mark.asyncio
async def test_album_download_reports_the_card_copy_a_renumbered_track_leaves_behind(
        client, db, monkeypatch, tmp_path, retag_calls):
    from routers import music as music_router

    album_dir = tmp_path / "library" / "The Killers" / "Hot Fuss"
    await _seed_done_track_with_sidecars(db, album_dir, "Mr. Brightside", youtube_id="vid2",
                                         artist="The Killers", album="Hot Fuss", album_artist="The Killers")
    monkeypatch.setattr(music_router.yt, "get_album_tracks", lambda bid: {
        "album": {"name": "Hot Fuss", "artist": "The Killers", "album_artist": "The Killers",
                  "image_url": None, "year": 2004, "total_tracks": 11, "total_discs": None},
        "tracks": [{"id": "vid2", "name": "Mr. Brightside", "artist": "The Killers", "track_number": 2,
                    "duration_ms": 1000, "album_artist": "The Killers", "year": 2004}],
    })

    resp = await client.post("/api/music/download/album/youtube/MPREb1", data={"format": "mp3"})

    assert resp.json()["details"][0]["stale_device_path"] == "The Killers/Hot Fuss/Mr. Brightside.m4a"


@pytest.mark.asyncio
async def test_spotify_album_detail_says_when_spotify_is_rate_limiting(client, db, monkeypatch):
    import time

    from services import spotify_client

    monkeypatch.setattr(spotify_client, "_rate_limited_until", time.monotonic() + 7200)

    resp = await client.get("/api/music/album/spotify/AL1")

    assert "Spotify is rate-limiting Leasure right now (try again in ~2 h). YouTube Music albums still work." in resp.text


@pytest.mark.asyncio
async def test_spotify_playlist_429_starts_the_rate_limit_window_and_says_so(client, db, monkeypatch):
    import spotipy

    from services import spotify_client

    class RateLimited:
        def playlist(self, *args, **kwargs):
            raise spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "600"})

        playlist_items = playlist

    monkeypatch.setattr(spotify_client, "_rate_limited_until", 0.0)
    monkeypatch.setattr(spotify_client, "get_client", lambda: RateLimited())

    resp = await client.get("/api/music/playlist/spotify/PL1")

    assert "try again in ~10 min" in resp.text


async def test_startup_does_not_claim_an_address_uvicorn_may_not_use(db, monkeypatch, caplog):
    from app import app
    from config import settings
    from worker import DownloadWorker

    monkeypatch.setattr("app.download_worker", DownloadWorker())
    monkeypatch.setattr(settings, "download_worker_enabled", False)
    caplog.set_level("INFO", logger="app")

    async with app.router.lifespan_context(app):
        pass

    assert "Leasure ready" in caplog.text and f":{settings.port}" not in caplog.text
