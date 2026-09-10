import json
from datetime import date

import pytest
import yt_dlp

from config import settings
from models import Track
from services import yt_engine
from services.ytdlp_opts import CookieSessionRefused

WANTED = {"title": "Blue Monday", "artist": "New Order", "duration_ms": 448_000}


def candidate(title="Blue Monday", artist="New Order", duration_s=448):
    return yt_engine.Candidate(video_id="vid", title=title, artist=artist, duration_s=duration_s)


def test_scorer_accepts_an_exact_match():
    assert yt_engine.score_candidate(candidate(), **WANTED) >= yt_engine.MIN_MATCH_SCORE


def test_scorer_rejects_a_remix():
    score = yt_engine.score_candidate(candidate(title="Blue Monday (Hardfloor Remix)"), **WANTED)
    assert score < yt_engine.MIN_MATCH_SCORE


def test_scorer_rejects_a_live_version():
    score = yt_engine.score_candidate(candidate(title="Blue Monday (Live at Reading)"), **WANTED)
    assert score < yt_engine.MIN_MATCH_SCORE


def test_scorer_keeps_a_variant_the_request_asked_for():
    wanted = {**WANTED, "title": "Blue Monday - Live"}
    score = yt_engine.score_candidate(candidate(title="Blue Monday (Live)"), **wanted)
    assert score >= yt_engine.MIN_MATCH_SCORE


def test_scorer_disqualifies_a_candidate_more_than_30s_off():
    assert yt_engine.score_candidate(candidate(duration_s=520), **WANTED) is None


def test_pick_best_returns_nothing_when_only_variants_match():
    candidates = [candidate(title="Blue Monday (Karaoke Version)"), candidate(title="Blue Monday 8D Audio")]
    assert yt_engine.pick_best(candidates, **WANTED) is None


class FakeYoutubeDL:
    """Writes a file where outtmpl points and reports a fixed info dict."""

    calls: list[dict] = []
    info: dict = {}
    error: Exception | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, url, download=True):
        FakeYoutubeDL.calls.append({"url": url, "opts": self.opts, "download": download})
        if FakeYoutubeDL.error:
            raise FakeYoutubeDL.error
        outtmpl = self.opts["outtmpl"]
        path = outtmpl.replace("%(ext)s", FakeYoutubeDL.info["requested_downloads"][0]["ext"])
        with open(path, "wb") as handle:
            handle.write(b"audio")
        info = json.loads(json.dumps(FakeYoutubeDL.info))
        info["requested_downloads"][0]["filepath"] = path
        return info


def _info(format_id, ext, acodec, abr, note="", duration=448.0):
    return {
        "title": "New Order - Blue Monday",
        "duration": duration,
        "requested_downloads": [
            {"format_id": format_id, "ext": ext, "acodec": acodec, "abr": abr,
             "asr": 44100, "format_note": note, "filesize": 5},
        ],
    }


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path):
    FakeYoutubeDL.calls = []
    FakeYoutubeDL.error = None
    FakeYoutubeDL.info = _info("141", "m4a", "mp4a.40.2", 255.8, note="Premium")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(yt_engine, "record_daily_download", lambda: 1)

    async def _no_tagging(path, track_id):
        return None

    import services.tagger as tagger

    monkeypatch.setattr(tagger, "apply_full_metadata", _no_tagging)
    return FakeYoutubeDL


async def _seed_track(db, **kwargs):
    fields = {"title": "Blue Monday", "artist": "New Order", "album": "Substance",
              "track_number": 3, "duration_ms": 448_000, "youtube_id": "vid", "quality": "native"}
    fields.update(kwargs)
    async with db() as session:
        track = Track(**fields)
        session.add(track)
        await session.commit()
        return track.id


async def _track(db, track_id):
    async with db() as session:
        return await session.get(Track, track_id)


async def test_premium_aac_download_records_real_provenance(db, fake_ytdlp):
    track_id = await _seed_track(db)

    path = await yt_engine.download(track_id)

    assert path.exists() and path.suffix == ".m4a"
    track = await _track(db, track_id)
    assert (track.quality, track.premium_used, track.source_format_id) == ("aac_256", True, "141")
    assert (track.source_codec, track.source_bitrate_kbps, track.source_sample_rate) == ("mp4a.40.2", 256, 44100)
    assert (track.format, track.transcoded, track.verification) == ("m4a", False, "ok")


async def test_standard_aac_download_is_labelled_128(db, fake_ytdlp):
    fake_ytdlp.info = _info("140", "m4a", "mp4a.40.2", 129.7)
    track_id = await _seed_track(db)

    await yt_engine.download(track_id)

    track = await _track(db, track_id)
    assert (track.quality, track.premium_used) == ("aac_128", False)


async def test_duration_mismatch_is_recorded_but_the_file_still_moves(db, fake_ytdlp):
    fake_ytdlp.info = _info("141", "m4a", "mp4a.40.2", 255.8, note="Premium", duration=408.0)
    track_id = await _seed_track(db)

    path = await yt_engine.download(track_id)

    assert path.exists()
    assert (await _track(db, track_id)).verification == "duration_mismatch"


async def test_staging_directory_is_removed_after_a_download(db, fake_ytdlp):
    track_id = await _seed_track(db)

    await yt_engine.download(track_id)

    assert not (settings.download_dir / str(track_id)).exists()
    assert not any(settings.download_dir.iterdir())


async def test_mp3_request_is_marked_transcoded(db, fake_ytdlp):
    fake_ytdlp.info = _info("141", "mp3", "mp3", 320.0, note="Premium")
    track_id = await _seed_track(db, quality="mp3_320")

    await yt_engine.download(track_id)

    track = await _track(db, track_id)
    assert (track.quality, track.transcoded, track.format) == ("mp3_320", True, "mp3")
    assert fake_ytdlp.calls[0]["opts"]["postprocessors"][0]["preferredcodec"] == "mp3"


async def test_a_refused_session_stops_without_a_second_attempt(db, fake_ytdlp):
    fake_ytdlp.error = RuntimeError("ERROR: Sign in to confirm you're not a bot")
    track_id = await _seed_track(db)

    with pytest.raises(CookieSessionRefused):
        await yt_engine.download(track_id)

    assert len(fake_ytdlp.calls) == 1


async def test_a_track_with_no_confident_match_fails_loudly(db, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(yt_engine, "search_ytmusic", lambda *a, **k: None)
    monkeypatch.setattr(yt_engine, "search_youtube", lambda *a, **k: None)
    track_id = await _seed_track(db, youtube_id=None, spotify_uri="spotify:track:x")

    with pytest.raises(yt_engine.DownloadFailed, match="No confident match"):
        await yt_engine.download(track_id)

    assert fake_ytdlp.calls == []


async def test_daily_cap_leaves_the_track_pending_and_queued(db, monkeypatch):
    from worker import DownloadWorker

    monkeypatch.setattr(yt_engine, "daily_cap_reached", lambda: True)
    monkeypatch.setattr("worker.CAP_RETRY_SECONDS", 0)
    called = []

    async def _never(track_id):
        called.append(track_id)

    monkeypatch.setattr("services.downloader.download_track", _never)
    track_id = await _seed_track(db, status="pending")
    worker = DownloadWorker()

    await worker._process_track(0, track_id)

    track = await _track(db, track_id)
    assert (track.status, called, worker.queue_size) == ("pending", [], 1)
    assert "cap" in track.error_message


def test_a_cookie_file_leaves_no_cookieless_retry(tmp_path, monkeypatch):
    from services import ytdlp_opts

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(settings, "cookie_file", cookie_file)

    attempts = ytdlp_opts.build_download_attempts({"format": "bestaudio"})

    assert len(attempts) == 1
    _, opts = attempts[0]
    assert opts["cookiefile"] == str(cookie_file)
    assert opts["sleep_interval"] == 10 and opts["ratelimit"] == 5_000_000


def test_daily_counter_resets_on_a_new_day(tmp_path, monkeypatch):
    counter = tmp_path / "yt_daily.json"
    counter.write_text(json.dumps({"date": "2000-01-01", "count": 99}))
    monkeypatch.setattr(yt_engine, "DAILY_COUNTER_FILE", counter)

    assert yt_engine.downloads_today() == 0
    assert yt_engine.record_daily_download() == 1
    assert json.loads(counter.read_text()) == {"date": date.today().isoformat(), "count": 1}


def test_a_missing_daily_counter_counts_as_no_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_engine, "DAILY_COUNTER_FILE", tmp_path / "yt_daily.json")

    assert yt_engine.downloads_today() == 0
