"""The download queue as the user sees it: failures, retry, dismiss, and format refusals."""
import pytest

from models import Track
from services import cookies
from services.formats import resolve_format


async def _add_track(db, **fields):
    async with db() as session:
        track = Track(**fields)
        session.add(track)
        await session.commit()
        await session.refresh(track)
        return track


@pytest.mark.asyncio
async def test_queue_shows_failed_downloads_with_their_message(client, db):
    await _add_track(db, title="Broken", artist="A", status="error", format="m4a",
                     error_message="Could not reach YouTube — check the network connection, then retry.")

    payload = (await client.get("/api/downloads/queue")).json()
    assert [t["status"] for t in payload["tracks"]] == ["error"]
    assert "Could not reach YouTube" in payload["tracks"][0]["error_message"]

    html = (await client.get("/api/downloads/queue/html")).text
    assert "Could not reach YouTube" in html
    assert "/api/downloads/retry/" in html and "/api/downloads/dismiss/" in html


@pytest.mark.asyncio
async def test_queue_orders_downloading_then_pending_then_error(client, db):
    await _add_track(db, title="C", artist="A", status="error")
    await _add_track(db, title="B", artist="A", status="pending")
    await _add_track(db, title="A", artist="A", status="downloading")

    tracks = (await client.get("/api/downloads/queue")).json()["tracks"]
    assert [t["status"] for t in tracks] == ["downloading", "pending", "error"]


@pytest.mark.asyncio
async def test_pending_track_held_by_the_cap_shows_its_message(client, db):
    await _add_track(db, title="Held", artist="A", status="pending", format="m4a",
                     error_message="Daily YouTube download cap reached (50 today) — this track stays queued.")

    html = (await client.get("/api/downloads/queue/html")).text
    assert "Daily YouTube download cap reached" in html
    assert "var(--color-error)" not in html


@pytest.mark.asyncio
async def test_retry_requeues_the_track_and_clears_the_error(client, db):
    track = await _add_track(db, title="Broken", artist="A", status="error", error_message="No confident match")

    resp = await client.post(f"/api/downloads/retry/{track.id}")
    assert resp.status_code == 200

    async with db() as session:
        retried = await session.get(Track, track.id)
        assert retried.status == "pending"
        assert retried.error_message is None

    from worker import download_worker
    assert download_worker.queue.get_nowait() == track.id


@pytest.mark.asyncio
async def test_dismiss_deletes_a_failed_track_that_has_no_file(client, db):
    track = await _add_track(db, title="Broken", artist="A", status="error", error_message="No confident match")

    resp = await client.post(f"/api/downloads/dismiss/{track.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.text == ""

    async with db() as session:
        assert await session.get(Track, track.id) is None


@pytest.mark.asyncio
async def test_dismiss_refuses_a_track_that_owns_a_library_file(client, db):
    track = await _add_track(db, title="Kept", artist="A", status="error", file_path="/library/A/Kept.m4a")

    resp = await client.post(f"/api/downloads/dismiss/{track.id}")
    assert resp.status_code == 400

    async with db() as session:
        assert await session.get(Track, track.id) is not None


def test_a_dropped_format_is_rejected_instead_of_becoming_mp3():
    assert resolve_format("native") == ("native", "m4a")
    with pytest.raises(ValueError, match="Format 'flac' is no longer available"):
        resolve_format("flac")


@pytest.mark.asyncio
async def test_download_with_a_dropped_format_is_a_400(client, db):
    resp = await client.post("/api/music/download/track",
                             data={"title": "T", "artist": "A", "youtube_id": "abc", "format": "flac"})
    assert resp.status_code == 400
    assert "no longer available" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_download_with_a_dropped_format_tells_htmx_callers_too(client, db):
    resp = await client.post("/api/music/download/track", headers={"HX-Request": "true"},
                             data={"title": "T", "artist": "A", "youtube_id": "abc", "format": "flac"})
    assert resp.status_code == 200
    assert "no longer available" in resp.text


@pytest.mark.asyncio
async def test_native_format_still_queues_a_download(client, db):
    resp = await client.post("/api/music/download/track",
                             data={"title": "T", "artist": "A", "youtube_id": "abc", "format": "native"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    async with db() as session:
        track = await session.get(Track, resp.json()["track_id"])
        assert (track.quality, track.format) == ("native", "m4a")


def test_premium_state_without_a_cookie_file_is_not_configured(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_file", None)

    assert cookies.premium_check_state()["state"] == "not_configured"


@pytest.mark.asyncio
async def test_premium_card_without_a_cookie_file_points_at_setup(client, monkeypatch):
    monkeypatch.setattr(cookies, "premium_check_state", lambda: {
        "premium": False, "formats": [], "error": "No cookie file yet: paste your browser headers.",
        "checked_at": None, "video_id": "abc", "state": "not_configured",
        "cookie": {"exists": False, "path": "/tmp/cookies.txt", "age_hours": None, "stale": True, "source": None}})

    resp = await client.get("/api/youtube/premium-check/html")
    assert "Premium: not checked" in resp.text
    assert "set up YouTube Music first" in resp.text
    assert "Premium: FAIL" not in resp.text


@pytest.mark.asyncio
async def test_premium_card_shows_when_the_check_last_ran(client, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(cookies, "premium_check", lambda video_id=None: {
        "premium": True, "formats": [], "error": None, "state": "pass",
        "checked_at": datetime.now(UTC).isoformat(), "video_id": "abc",
        "cookie": {"exists": True, "path": "/tmp/cookies.txt", "age_hours": 1.0, "stale": False,
                   "source": "headers"}})

    resp = await client.post("/api/youtube/premium-check/run", headers={"HX-Request": "true"})
    assert "Premium: PASS" in resp.text
    assert "Checked just now" in resp.text


@pytest.mark.asyncio
async def test_carousel_shows_placeholders_for_a_library_without_artwork(client, db):
    await _add_track(db, title="a", artist="A", album="First", status="done")
    await _add_track(db, title="b", artist="A", album="Second", status="done")

    resp = await client.get("/api/home/carousel")
    assert resp.text.count("lp-cover-placeholder") == 2
    assert "2 tracks across 2 albums<" in resp.text
    assert "no music yet" not in resp.text


@pytest.mark.asyncio
async def test_a_failed_download_keeps_the_message_the_engine_wrote(db, monkeypatch):
    from services import downloader, yt_engine
    from worker import DownloadWorker

    monkeypatch.setattr(yt_engine, "daily_cap_reached", lambda: False)

    async def fail(track_id):
        raise yt_engine.DownloadFailed("No confident match on YouTube for 'A - T'.")

    monkeypatch.setattr(downloader, "download_track", fail)
    track = await _add_track(db, title="T", artist="A", status="pending")

    await DownloadWorker()._process_track(0, track.id)

    async with db() as session:
        failed = await session.get(Track, track.id)
    assert failed.status == "error"
    assert failed.error_message == "No confident match on YouTube for 'A - T'."


@pytest.mark.asyncio
async def test_a_track_with_no_source_says_so_instead_of_returning_nothing(db):
    from services.downloader import download_track
    from services.yt_engine import DownloadFailed

    track = await _add_track(db, title="T", artist="A", status="pending", quality="mp3_320")

    with pytest.raises(DownloadFailed, match="no Spotify or YouTube source"):
        await download_track(track.id)


def test_an_unreachable_network_is_named_as_the_reason():
    from services.yt_engine import _download_failure

    failure = _download_failure(RuntimeError("ERROR: unable to download: <urlopen error [Errno -3] "
                                             "Temporary failure in name resolution>"))
    assert "network connection" in str(failure)
