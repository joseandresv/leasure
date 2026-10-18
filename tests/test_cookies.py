import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import settings
from services import cookies
from services.ytdlp_opts import ensure_deno_on_path

HEADER_COOKIE = (
    "SAPISID=sapi-value; __Secure-3PAPISID=secure-value; LOGIN_INFO=login-value; "
    "YSC=ysc-value; DEVICE_INFO=tracking-junk; _ga=analytics-junk"
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch, isolated_paths):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_file", None)
    isolated_paths()
    return tmp_path


def _write_meta(data_dir, source, written_at):
    body = (data_dir / "cookies.txt").read_bytes()
    (data_dir / "cookies.txt.meta.json").write_text(json.dumps(
        {"source": source, "written_at": written_at.isoformat(),
         "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}))


def _write_headers(data_dir, cookie_header=HEADER_COOKIE):
    path = data_dir / "youtube_headers.json"
    path.write_text(json.dumps({"cookie": cookie_header, "x-goog-authuser": "0"}))
    return path


def test_header_cookies_become_netscape_lines(data_dir):
    path = cookies.write_cookie_file_from_headers(_write_headers(data_dir))

    lines = path.read_text().splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    fields = [line.split("\t") for line in lines[1:]]
    assert [".youtube.com", "TRUE", "/", "TRUE"] == fields[0][:4]
    assert int(fields[0][4]) > time.time() + 179 * 86400
    assert ("SAPISID", "sapi-value") == (fields[0][5], fields[0][6])
    assert ".google.com\tTRUE\t/\tTRUE" in path.read_text()


def test_only_allow_listed_cookie_names_are_written(data_dir):
    path = cookies.write_cookie_file_from_headers(_write_headers(data_dir))

    names = {line.split("\t")[5] for line in path.read_text().splitlines()[1:]}
    assert names == {"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO", "YSC"}


def test_cookie_file_is_not_written_without_auth_cookies(data_dir):
    assert cookies.write_cookie_file_from_headers(_write_headers(data_dir, "DEVICE_INFO=junk")) is None
    assert not cookies.cookie_file_path().exists()


def test_status_reports_source_and_freshness(data_dir):
    assert cookies.cookie_file_status() == {"exists": False, "path": str(data_dir / "cookies.txt"),
                                            "age_hours": None, "stale": True, "source": None}

    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fresh = cookies.cookie_file_status()
    assert fresh["exists"] and fresh["source"] == "headers" and not fresh["stale"]


def test_status_ages_the_file_from_the_meta_timestamp_not_the_mtime(data_dir):
    """yt-dlp rewrites the jar on every download, so a fresh mtime hides an old session."""
    path = cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    _write_meta(data_dir, "headers", datetime.now(UTC) - timedelta(hours=20))
    os.utime(path, None)

    status = cookies.cookie_file_status()
    assert status["stale"] and status["age_hours"] == pytest.approx(20, abs=0.2)


def test_status_falls_back_to_the_mtime_without_a_meta_file(data_dir):
    path = data_dir / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n")
    old = time.time() - 20 * 3600
    os.utime(path, (old, old))

    status = cookies.cookie_file_status()
    assert status["stale"] and status["age_hours"] == pytest.approx(20, abs=0.2)


def test_status_calls_a_dropped_in_file_external(data_dir):
    (data_dir / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    assert cookies.cookie_file_status()["source"] == "external"


def test_the_meta_file_fingerprints_the_cookie_file_it_describes(data_dir):
    path = cookies.write_cookie_file_from_headers(_write_headers(data_dir))

    meta = json.loads((data_dir / "cookies.txt.meta.json").read_text())
    assert meta["size"] == path.stat().st_size
    assert meta["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_status_trusts_a_legacy_meta_that_has_no_fingerprint(data_dir):
    """Meta files written before fingerprints existed still date the session."""
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    written_at = datetime.now(UTC) - timedelta(hours=8)
    (data_dir / "cookies.txt.meta.json").write_text(
        json.dumps({"source": "app-window", "written_at": written_at.isoformat()}))

    status = cookies.cookie_file_status()
    assert status["source"] == "app-window"
    assert status["age_hours"] == pytest.approx(8, abs=0.2)


def test_status_distrusts_the_meta_of_a_hand_replaced_cookie_file(data_dir):
    path = cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    path.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tpasted-by-hand\n")
    old = time.time() - 20 * 3600
    os.utime(path, (old, old))

    status = cookies.cookie_file_status()
    assert status["source"] == "external"
    assert status["stale"] and status["age_hours"] == pytest.approx(20, abs=0.2)


def test_a_jar_rewrite_by_yt_dlp_keeps_the_recorded_source_and_age(data_dir):
    """yt-dlp saves the jar back after every run; that is our own session, not a hand edit."""
    path = cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    _write_meta(data_dir, "headers", datetime.now(UTC) - timedelta(hours=5))
    path.write_text(path.read_text() + ".youtube.com\tTRUE\t/\tTRUE\t0\tSIDCC\trotated\n")

    cookies.note_cookie_file_rewritten()

    status = cookies.cookie_file_status()
    assert status["source"] == "headers" and status["age_hours"] == pytest.approx(5, abs=0.2)


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; records the URL and returns canned formats."""

    calls: list[tuple[dict, str]] = []
    formats: list[dict] = []
    raises: Exception | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        _FakeYDL.calls.append((self.opts, url))
        if _FakeYDL.raises:
            raise _FakeYDL.raises
        return {"formats": _FakeYDL.formats}


@pytest.fixture
def fake_ydl(monkeypatch):
    import yt_dlp

    _FakeYDL.calls = []
    _FakeYDL.formats = []
    _FakeYDL.raises = None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    return _FakeYDL


def _audio(format_id, abr, note="", acodec="mp4a.40.2"):
    return {"format_id": format_id, "abr": abr, "format_note": note, "acodec": acodec, "vcodec": "none"}


def test_premium_check_passes_when_premium_itag_is_offered(data_dir, fake_ydl):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fake_ydl.formats = [_audio("140", 128), _audio("141", 256, "Premium")]

    result = cookies.premium_check("abc123")

    assert result["premium"] is True
    assert result["error"] is None
    assert result["formats"][0]["format_id"] == "141"
    opts, url = fake_ydl.calls[0]
    assert url == "https://music.youtube.com/watch?v=abc123"
    assert opts["cookiefile"] == str(cookies.cookie_file_path())
    assert opts["extractor_args"]["youtube"]["player_client"] == ["web_music"]


def test_premium_check_fails_when_only_standard_audio_is_offered(data_dir, fake_ydl):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fake_ydl.formats = [_audio("140", 128), _audio("251", 130, acodec="opus")]

    result = cookies.premium_check()

    assert result["premium"] is False
    assert "128" in result["error"]


def test_premium_check_without_a_cookie_file_says_how_to_get_one(data_dir, fake_ydl):
    result = cookies.premium_check()

    assert result["premium"] is False
    assert "paste your browser headers" in result["error"]
    assert str(cookies.cookie_file_path()) in result["error"]
    assert fake_ydl.calls == []


def test_premium_check_explains_a_bot_check_failure(data_dir, fake_ydl):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fake_ydl.raises = RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot.")

    result = cookies.premium_check()

    assert result["premium"] is False
    assert "not a bot" in result["error"] and "fresh headers" in result["error"]


def test_premium_check_falls_back_when_the_probe_track_is_gone(data_dir, fake_ydl, monkeypatch):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fallback = cookies.PREMIUM_CHECK_FALLBACK_IDS[0]
    probed = []

    def fake_extract(video_id, cookie_path):
        probed.append(video_id)
        if video_id != fallback:
            raise Exception(f"ERROR: [youtube] {video_id}: Video unavailable")
        return {"formats": [_audio("141", 256, "Premium")]}

    monkeypatch.setattr(cookies, "_extract_info", fake_extract)

    result = cookies.premium_check()

    assert probed == [settings.premium_check_video_id, fallback]
    assert result["state"] == "pass"
    assert result["video_id"] == fallback


def test_premium_check_does_not_fall_back_after_a_rejected_session(data_dir, fake_ydl, monkeypatch):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    probed = []

    def fake_extract(video_id, cookie_path):
        probed.append(video_id)
        raise Exception("ERROR: unable to download video data: HTTP Error 403: Forbidden")

    monkeypatch.setattr(cookies, "_extract_info", fake_extract)

    result = cookies.premium_check()

    assert probed == [settings.premium_check_video_id]
    assert result["state"] == "error"
    assert "403" in result["error"]


def test_premium_check_blames_a_missing_js_runtime_when_there_are_no_formats(data_dir, fake_ydl):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fake_ydl.formats = []

    result = cookies.premium_check()

    assert result["state"] == "error"
    assert "deno" in result["error"]
    assert fake_ydl.calls[0][0]["ignore_no_formats_error"] is True


def test_premium_check_result_is_cached_for_the_status_card(data_dir, fake_ydl):
    cookies.write_cookie_file_from_headers(_write_headers(data_dir))
    fake_ydl.formats = [_audio("141", 256, "Premium")]
    cookies.premium_check()

    state = cookies.premium_check_state()
    assert state["premium"] is True
    assert state["cookie"]["source"] == "headers"


def test_premium_check_state_is_unchecked_before_the_first_run(data_dir):
    assert cookies.premium_check_state()["premium"] is None


@pytest.mark.asyncio
async def test_premium_check_endpoint_returns_the_cached_verdict(client, monkeypatch):
    state = {"premium": True, "formats": [], "cookie": cookies.cookie_file_status(),
             "error": None, "checked_at": "2026-09-10T00:00:00+00:00", "video_id": "abc"}
    monkeypatch.setattr(cookies, "premium_check_state", lambda: state)

    resp = await client.get("/api/youtube/premium-check")
    assert resp.status_code == 200
    assert resp.json()["premium"] is True


@pytest.mark.asyncio
async def test_premium_check_html_reports_the_verdict_in_plain_words(client, monkeypatch):
    monkeypatch.setattr(cookies, "premium_check_state", lambda: {
        "premium": False, "formats": [], "error": "Nope.", "checked_at": None, "video_id": "abc",
        "cookie": {"exists": True, "path": "/tmp/cookies.txt", "age_hours": 3.0, "stale": False, "source": "headers"}})

    resp = await client.get("/api/youtube/premium-check/html")
    assert resp.status_code == 200
    assert "Premium: FAIL" in resp.text
    assert "Cookies from pasted headers, 3.0 h old" in resp.text


@pytest.mark.asyncio
async def test_premium_check_run_is_a_post_that_re_runs_the_check(client, monkeypatch):
    ran = []

    def fake_check(video_id=None):
        ran.append(video_id)
        return {"premium": True, "formats": [{"format_id": "141", "acodec": "mp4a.40.2", "abr": 256,
                                              "format_note": "Premium"}],
                "cookie": {"exists": True, "path": "/tmp/cookies.txt", "age_hours": 1.0, "stale": False,
                           "source": "headers"},
                "error": None, "checked_at": "2026-09-10T00:00:00+00:00", "video_id": "abc"}

    monkeypatch.setattr(cookies, "premium_check", fake_check)

    resp = await client.post("/api/youtube/premium-check/run", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Premium: PASS" in resp.text
    assert "141 mp4a.40.2 256k" in resp.text
    assert ran == [None]

    assert (await client.get("/api/youtube/premium-check/run")).status_code == 405


def test_ensure_deno_on_path_adds_the_bin_dir_once(tmp_path, monkeypatch):
    deno_bin = tmp_path / ".deno" / "bin"
    deno_bin.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")

    ensure_deno_on_path()
    ensure_deno_on_path()

    assert os.environ["PATH"] == f"{deno_bin}{os.pathsep}/usr/bin"


def test_ensure_deno_on_path_recognises_an_entry_with_a_trailing_separator(tmp_path, monkeypatch):
    deno_bin = tmp_path / ".deno" / "bin"
    deno_bin.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", f"{deno_bin}{os.sep}{os.pathsep}/usr/bin")

    ensure_deno_on_path()

    assert os.environ["PATH"] == f"{deno_bin}{os.sep}{os.pathsep}/usr/bin"
