from pathlib import Path

import pytest

from services import downloader, qobuz_engine, yt_engine
from services.qobuz_engine import QobuzSkip
from services.yt_engine import CapDeferred, DownloadFailed
from tests.qobuz_fakes import get_track, seed_track


@pytest.fixture
def engines(monkeypatch):
    """Both engines replaced by recorders; `qobuz` decides what the Qobuz attempt does."""
    calls = {"qobuz": [], "youtube": [], "qobuz_outcome": Path("/library/q.flac")}

    async def fake_qobuz(track_id):
        calls["qobuz"].append(track_id)
        if isinstance(calls["qobuz_outcome"], Exception):
            raise calls["qobuz_outcome"]
        return calls["qobuz_outcome"]

    async def fake_youtube(track_id):
        calls["youtube"].append(track_id)
        return Path("/library/y.m4a")

    monkeypatch.setattr(qobuz_engine, "qobuz_download", fake_qobuz)
    monkeypatch.setattr(yt_engine, "download", fake_youtube)
    monkeypatch.setattr(yt_engine, "daily_cap_reached", lambda: False)
    return calls


async def test_best_uses_qobuz_when_it_delivers(db, engines):
    track_id = await seed_track(db, quality="native")

    assert await downloader.download_track(track_id) == Path("/library/q.flac")
    assert engines["youtube"] == []


async def test_best_falls_back_to_youtube_and_stores_why(db, engines):
    engines["qobuz_outcome"] = QobuzSkip("no_match")
    track_id = await seed_track(db, quality="native")

    assert await downloader.download_track(track_id) == Path("/library/y.m4a")
    assert (await get_track(db, track_id)).qobuz_skip_reason == "no_match"


async def test_mp3_never_asks_qobuz(db, engines):
    track_id = await seed_track(db, quality="mp3_320")

    await downloader.download_track(track_id)

    assert engines["qobuz"] == [] and engines["youtube"] == [track_id]


async def test_flac_only_fails_instead_of_falling_back(db, engines):
    engines["qobuz_outcome"] = QobuzSkip("not_configured")
    track_id = await seed_track(db, quality="flac_lossless")

    with pytest.raises(DownloadFailed, match="not signed in"):
        await downloader.download_track(track_id)

    assert engines["youtube"] == []


async def test_the_youtube_cap_does_not_hold_back_a_qobuz_download(db, engines, monkeypatch):
    monkeypatch.setattr(yt_engine, "daily_cap_reached", lambda: True)
    track_id = await seed_track(db, quality="flac_lossless")

    assert await downloader.download_track(track_id) == Path("/library/q.flac")


async def test_best_with_qobuz_skipped_and_the_youtube_cap_reached_waits(db, engines, monkeypatch):
    monkeypatch.setattr(yt_engine, "daily_cap_reached", lambda: True)
    engines["qobuz_outcome"] = QobuzSkip("cap")
    track_id = await seed_track(db, quality="native")

    with pytest.raises(CapDeferred, match="YouTube download cap"):
        await downloader.download_track(track_id)


async def test_a_qobuz_failure_does_not_fall_back(db, engines):
    engines["qobuz_outcome"] = DownloadFailed("The Qobuz download is damaged or incomplete — retry.")
    track_id = await seed_track(db, quality="native")

    with pytest.raises(DownloadFailed, match="damaged"):
        await downloader.download_track(track_id)

    assert engines["youtube"] == []


async def test_a_result_tier_is_never_downloaded_as_mp3(db, engines):
    track_id = await seed_track(db, quality="flac_24_96")

    with pytest.raises(DownloadFailed, match="unknown format request"):
        await downloader.download_track(track_id)

    assert engines["qobuz"] == [] and engines["youtube"] == []


async def test_retrying_a_finished_track_is_refused(client, db):
    track_id = await seed_track(db, status="done", file_path="/library/x.m4a")

    resp = await client.post(f"/api/downloads/retry/{track_id}")

    assert resp.status_code == 409
    assert (await get_track(db, track_id)).status == "done"
