import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture

from config import settings
from services import qobuz_client, qobuz_engine, qobuz_session, tagger
from services.qobuz_engine import QobuzSkip
from services.yt_engine import CapDeferred, DownloadFailed
from tests.qobuz_fakes import FakeClient, ffmpeg_path, get_track, make_flac, qtrack, seed_track, store_session

FFMPEG = ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


@pytest.fixture
def tagged(monkeypatch):
    """apply_full_metadata stand-in: records its calls and writes a .jpg sidecar next to the staged file."""
    calls = []

    async def fake_apply(path, track_id, *, artwork_url_override=None):
        calls.append({"path": Path(path), "track_id": track_id, "override": artwork_url_override})
        Path(path).with_suffix(".jpg").write_bytes(b"jpg")

    monkeypatch.setattr(tagger, "apply_full_metadata", fake_apply)
    return calls


@pytest.fixture
def client(monkeypatch):
    """A signed-in session whose client is the scripted FakeClient; the full decode trusts STREAMINFO."""
    store_session()
    shutil.rmtree(settings.library_dir / "Miles Davis", ignore_errors=True)
    fake = FakeClient()
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: fake)
    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", lambda path: FLAC(path).info.length)
    return fake


async def test_a_qobuz_download_records_flac_provenance(db, client, tagged):
    client.search = [qtrack(isrc="USSM15900113")]
    track_id = await seed_track(db)

    path = await qobuz_engine.qobuz_download(track_id)

    track = await get_track(db, track_id)
    assert path.suffix == ".flac" and path.exists() and path.with_suffix(".jpg").exists()
    assert (track.engine_used, track.quality, track.source_format_id) == ("qobuz", "flac_24_96", "7")
    assert (track.source_bit_depth, track.source_sample_rate, track.is_lossless) == (24, 96000, True)
    assert (track.qobuz_match_method, track.qobuz_isrc, track.isrc) == ("search", "USSM15900113", None)
    assert not (settings.download_dir / str(track_id)).exists()


async def test_an_id_match_fills_a_missing_isrc(db, client, tagged):
    client.by_id = {"q7": qtrack(id="q7", isrc="USSM15900113")}
    track_id = await seed_track(db, qobuz_id="q7", qobuz_match_method="id")

    await qobuz_engine.qobuz_download(track_id)

    assert (await get_track(db, track_id)).isrc == "USSM15900113"


async def test_the_tagger_runs_on_the_staged_file_with_the_qobuz_cover(db, client, tagged):
    cover = "https://static.qobuz.com/images/covers/ab/cd/x_600.jpg"
    client.search = [qtrack(cover_url=cover)]
    track_id = await seed_track(db, artwork_url="https://i.ytimg.com/vi/x/hq.jpg")

    await qobuz_engine.qobuz_download(track_id)

    (call,) = tagged
    assert call["path"] == settings.download_dir / str(track_id) / "source.flac"
    assert call["override"] == cover
    assert (await get_track(db, track_id)).artwork_url == cover


def test_strip_removes_shipped_tags_pictures_and_id3(tmp_path):
    path = make_flac(tmp_path / "x.flac", seconds=5)
    audio = FLAC(path)
    audio["title"] = "Qobuz title"
    picture = Picture()
    picture.data = b"img"
    audio.add_picture(picture)
    audio.save()
    path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + path.read_bytes())

    qobuz_engine.strip_shipped_tags(path)

    clean = FLAC(path)
    assert not clean.tags and clean.pictures == [] and not path.read_bytes().startswith(b"ID3")


@needs_ffmpeg
def test_a_truncated_flac_fails_the_full_decode(tmp_path):
    whole = tmp_path / "whole.flac"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=3:a=0.3", "-ac", "2",
                    "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac", str(whole)], check=True)
    cut = tmp_path / "cut.flac"
    cut.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])

    with pytest.raises(DownloadFailed, match="damaged or incomplete"):
        qobuz_engine.verify_flac(cut, 3000, 3.0)


@needs_ffmpeg
@pytest.mark.parametrize("rate, fmt, tier", [(44100, "s16", "flac_16_44"), (96000, "s32", "flac_24_96")])
def test_real_flac_files_get_their_tier(tmp_path, rate, fmt, tier):
    path = tmp_path / "real.flac"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=3:a=0.3", "-ac", "2",
                    "-ar", str(rate), "-sample_fmt", fmt, "-c:a", "flac", str(path)], check=True)

    facts = qobuz_engine.verify_flac(path, 3000, 3.0)

    assert qobuz_engine.quality_tier(facts.bits, facts.sample_rate) == tier
    assert abs(facts.decoded_s - 3.0) < 0.1


async def test_a_truncated_download_leaves_no_library_file(db, client, tagged, monkeypatch):
    def damaged(_path):
        raise DownloadFailed(qobuz_engine.DAMAGED_MESSAGE)

    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", damaged)
    track_id = await seed_track(db)

    with pytest.raises(DownloadFailed, match="damaged or incomplete"):
        await qobuz_engine.qobuz_download(track_id)

    assert not any(settings.library_dir.rglob("*.flac"))
    assert not (settings.download_dir / str(track_id)).exists()


def test_six_channels_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", lambda path: 200.0)

    with pytest.raises(DownloadFailed, match="6 channels"):
        qobuz_engine.verify_flac(make_flac(tmp_path / "x.flac", channels=6), 200_000, 200.0)


async def test_352_khz_is_downloaded_again_at_format_7(db, client, tagged):
    def writer(dest, max_format_id):
        make_flac(dest, sample_rate=352800 if max_format_id == 27 else 96000)

    client.writer = writer
    track_id = await seed_track(db)

    await qobuz_engine.qobuz_download(track_id)

    assert [fmt for _, fmt in client.download_calls] == [27, 7]


async def test_a_not_streamable_candidate_gives_way_to_the_next(db, client, tagged):
    client.search = [qtrack(id="first", album_title="Kind of Blue"), qtrack(id="second", album_title="Other")]
    client.resolve_errors = {"first": qobuz_client.QobuzNotStreamableError(reason="restricted")}
    track_id = await seed_track(db)

    await qobuz_engine.qobuz_download(track_id)

    assert (await get_track(db, track_id)).qobuz_id == "second"
    assert qobuz_engine.downloads_today() == 1


async def test_one_slot_left_lets_exactly_one_of_two_downloads_fetch(db, client, tagged, monkeypatch):
    monkeypatch.setattr(settings, "qobuz_max_downloads_per_day", 1)
    first, second = await seed_track(db), await seed_track(db, title="So What", youtube_id="yt2")

    results = await asyncio.gather(qobuz_engine.qobuz_download(first), qobuz_engine.qobuz_download(second),
                                   return_exceptions=True)

    skips = [r for r in results if isinstance(r, QobuzSkip)]
    assert len(skips) == 1 and skips[0].reason == "cap"
    assert qobuz_engine.downloads_today() == 1


async def test_a_failed_verify_still_used_its_cap_slot(db, client, tagged, monkeypatch):
    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", lambda _p: 1.0)

    with pytest.raises(DownloadFailed):
        await qobuz_engine.qobuz_download(await seed_track(db))

    assert qobuz_engine.downloads_today() == 1


async def test_too_little_disk_defers_the_track(db, client, tagged, monkeypatch):
    class Usage:
        free = 10 * 1024**2

    monkeypatch.setattr(qobuz_engine.shutil, "disk_usage", lambda _p: Usage())

    with pytest.raises(CapDeferred) as deferred:
        await qobuz_engine.qobuz_download(await seed_track(db))

    assert deferred.value.retry_after_s == 900 and qobuz_engine.downloads_today() == 0


async def test_a_429_cooldown_survives_a_fresh_module_state(db, client, tagged):
    client.resolve_errors = {"q1": qobuz_client.QobuzRateLimitError("slow down", retry_after=120)}

    with pytest.raises(CapDeferred):
        await qobuz_engine.qobuz_download(await seed_track(db))
    qobuz_engine._reset_for_tests()

    assert "cooldown_until" in json.loads(qobuz_engine.DAILY_FILE.read_text())
    assert 100 < qobuz_engine.cooldown_left() <= 120
    assert qobuz_engine.downloads_today() == 0


async def test_a_broken_app_opens_the_breaker_and_best_falls_back(db, client, tagged):
    client.resolve_errors = {"q1": qobuz_client.QobuzAppError("Qobuz changed its web player.")}
    with pytest.raises(QobuzSkip, match="paused Qobuz"):
        await qobuz_engine.qobuz_download(await seed_track(db))
    client.download_calls.clear()

    with pytest.raises(QobuzSkip) as skipped:
        await qobuz_engine.qobuz_download(await seed_track(db, youtube_id="yt2"))
    with pytest.raises(CapDeferred):
        await qobuz_engine.qobuz_download(await seed_track(db, youtube_id="yt3", quality="flac_lossless"))

    assert skipped.value.reason == "unavailable" and client.download_calls == []
    assert qobuz_engine.downloads_today() == 0


async def test_three_cdn_failures_open_the_breaker(db, client, tagged):
    client.errors = {"q1": qobuz_client.QobuzDownloadError("The Qobuz download failed on the network.")}
    for _ in range(3):
        with pytest.raises(DownloadFailed):
            await qobuz_engine.qobuz_download(await seed_track(db))

    assert qobuz_engine.breaker_state()["reason"] == "cdn_unavailable"


async def test_a_confirmed_401_expires_this_session(db, client, tagged, monkeypatch):
    expired = []
    monkeypatch.setattr(qobuz_session, "mark_expired", lambda message, fp: expired.append(fp))
    client.resolve_errors = {"q1": qobuz_client.QobuzAuthError("refused")}

    with pytest.raises(QobuzSkip) as skipped:
        await qobuz_engine.qobuz_download(await seed_track(db))

    assert skipped.value.reason == "signed_out" and expired == [client.token_fingerprint]
    assert qobuz_engine.downloads_today() == 0


async def test_no_session_skips_without_a_client(db, monkeypatch):
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: pytest.fail("no client expected"))

    with pytest.raises(QobuzSkip) as skipped:
        await qobuz_engine.qobuz_download(await seed_track(db))

    assert skipped.value.reason == "not_configured"


async def test_the_track_gap_is_shared_between_workers(monkeypatch):
    monkeypatch.setattr(qobuz_engine, "TRACK_GAP_S", (0.3, 0.3))
    store_session()
    started = []

    async def through_gate():
        await qobuz_engine._gate("native")
        started.append(time.monotonic())

    await asyncio.gather(through_gate(), through_gate())

    assert started[1] - started[0] >= 0.25


async def test_a_preview_body_is_refused_after_using_its_slot(db, client, tagged):
    client.flac = {"sample_rate": 96000, "bits": 24, "seconds": 30.0}
    track_id = await seed_track(db, duration_ms=200_000)

    with pytest.raises(DownloadFailed, match="30-second preview"):
        await qobuz_engine.qobuz_download(track_id)

    assert not any(settings.library_dir.rglob("*.flac"))
    assert not (settings.download_dir / str(track_id)).exists()
    assert qobuz_engine.downloads_today() == 1


async def test_three_damaged_bodies_open_the_breaker_and_best_falls_back(db, client, tagged, monkeypatch):
    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", lambda _p: 1.0)
    for _ in range(3):
        with pytest.raises(DownloadFailed, match="damaged or incomplete"):
            await qobuz_engine.qobuz_download(await seed_track(db))

    with pytest.raises(QobuzSkip) as skipped:
        await qobuz_engine.qobuz_download(await seed_track(db))

    assert skipped.value.reason == "cdn_unavailable"


async def test_a_token_qobuz_rotated_during_a_download_is_stored(db, client, tagged):
    client.rotated_token = "tok_new_" + "X" * 30

    await qobuz_engine.qobuz_download(await seed_track(db))

    stored = qobuz_session.load()
    assert stored.user_auth_token == client.rotated_token and stored.rotated_at


async def test_a_rotated_token_never_overwrites_a_newer_sign_in(db, client, tagged):
    newer = "tok_newer_" + "Y" * 30
    client.rotated_token = "tok_new_" + "X" * 30

    def sign_in_meanwhile(dest, _max_format_id):
        store_session(user_auth_token=newer)
        make_flac(dest)

    client.writer = sign_in_meanwhile

    await qobuz_engine.qobuz_download(await seed_track(db))

    assert qobuz_session.load().user_auth_token == newer


async def test_a_download_never_lands_on_a_stem_another_track_owns(db, client, tagged):
    album_dir = settings.library_dir / "Miles Davis" / "Kind of Blue"
    other = album_dir / "01 - So What.m4a"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"audio")
    other.with_suffix(".lrc").write_text("other lyrics")
    await seed_track(db, youtube_id="yt0", status="done", file_path=str(other))

    with pytest.raises(DownloadFailed, match="already saved"):
        await qobuz_engine.qobuz_download(await seed_track(db))

    assert sorted(p.name for p in album_dir.iterdir()) == ["01 - So What.lrc", "01 - So What.m4a"]
    assert other.with_suffix(".lrc").read_text() == "other lyrics"
