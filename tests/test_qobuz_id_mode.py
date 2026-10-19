"""Qobuz favorites download by their exact id (no search, no title gates) and the cap's per-kind shares."""
import json
import shutil

import pytest
from mutagen.flac import FLAC

from config import settings
from services import qobuz_engine, qobuz_session, tagger
from services.qobuz_engine import QobuzSkip, TrackSnapshot, resolve_matches
from services.yt_engine import CapDeferred
from tests.qobuz_fakes import FakeClient, get_track, qtrack, seed_track, store_session


class NoSearchClient(FakeClient):
    def search_tracks(self, query, limit=10):
        raise AssertionError("id mode must not search")

    def find_by_isrc(self, isrc, limit=10):
        raise AssertionError("id mode must not look up ISRCs")


def favorite(title="So What", duration_ms=200_000, qobuz_id="q9"):
    return TrackSnapshot(id=1, title=title, artist="Miles Davis", album="Kind of Blue", album_artist=None,
                         duration_ms=duration_ms, isrc="USSM15900113", qobuz_id=qobuz_id, qobuz_match_method="id",
                         artwork_url=None, file_path=None, quality="flac_lossless", source="qobuz")


def test_a_favorite_whose_id_is_gone_is_no_match_without_searching():
    with pytest.raises(QobuzSkip) as skipped:
        resolve_matches(NoSearchClient(), favorite(), strict=False)
    assert skipped.value.reason == "no_match"


@pytest.mark.parametrize("version", ["Live at Newport", "2011 Remaster"])
def test_a_versioned_favorite_passes_on_its_id(version):
    client = NoSearchClient(by_id={"q9": qtrack(id="q9", title="So What", version=version)})
    matches = resolve_matches(client, favorite(title=f"So What ({version})"), strict=False)
    assert [(m.track.id, m.method) for m in matches] == [("q9", "id")]


def test_a_non_streamable_favorite_reports_not_streamable():
    client = NoSearchClient(by_id={"q9": qtrack(id="q9", streamable=False)})
    with pytest.raises(QobuzSkip) as skipped:
        resolve_matches(client, favorite(), strict=False)
    assert skipped.value.reason == "not_streamable"


def test_an_id_now_pointing_at_other_audio_is_no_match():
    client = NoSearchClient(by_id={"q9": qtrack(id="q9", duration_s=210)})
    with pytest.raises(QobuzSkip) as skipped:
        resolve_matches(client, favorite(duration_ms=200_000), strict=False)
    assert skipped.value.reason == "no_match"


@pytest.fixture
def qobuz(monkeypatch):
    store_session()
    shutil.rmtree(settings.library_dir / "Miles Davis", ignore_errors=True)
    fake = NoSearchClient(by_id={"q9": qtrack(id="q9", title="So What", version="Live")})
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: fake)
    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", lambda path: FLAC(path).info.length)

    async def fake_apply(path, track_id, *, artwork_url_override=None):
        return None

    monkeypatch.setattr(tagger, "apply_full_metadata", fake_apply)
    return fake


async def test_a_favorite_download_keeps_the_id_provenance(db, qobuz):
    track_id = await seed_track(db, title="So What (Live)", source="qobuz", qobuz_id="q9", qobuz_match_method="id",
                                youtube_id=None, quality="flac_lossless")

    await qobuz_engine.qobuz_download(track_id)

    track = await get_track(db, track_id)
    assert (track.engine_used, track.qobuz_id, track.qobuz_match_method) == ("qobuz", "q9", "id")


async def test_a_bulk_slot_is_recorded_by_kind(db, qobuz):
    track_id = await seed_track(db, source="qobuz", qobuz_id="q9", youtube_id=None, quality="flac_lossless")

    await qobuz_engine.qobuz_download(track_id, kind="bulk")

    state = json.loads(qobuz_engine.DAILY_FILE.read_text())
    assert (state["count"], state["by_kind"]) == (1, {"bulk": 1})


def test_the_bulk_share_stops_bulk_but_not_clicks(monkeypatch):
    monkeypatch.setitem(qobuz_engine.DAILY_SHARES, "bulk", 0.02)

    assert [qobuz_engine.reserve_daily_slot("bulk") for _ in range(3)] == [True, True, False]
    assert qobuz_engine.reserve_daily_slot("interactive") is True
    assert (qobuz_engine.used_today("bulk"), qobuz_engine.downloads_today()) == (2, 3)


async def test_a_used_up_bulk_share_holds_a_flac_only_bulk_row(db, qobuz, monkeypatch):
    monkeypatch.setitem(qobuz_engine.DAILY_SHARES, "bulk", 0.0)
    track_id = await seed_track(db, source="qobuz", qobuz_id="q9", youtube_id=None, quality="flac_lossless")

    with pytest.raises(CapDeferred, match="share for Download all"):
        await qobuz_engine.qobuz_download(track_id, kind="bulk")

    assert qobuz.download_calls == []


def test_a_cooldown_keeps_todays_kind_counts():
    qobuz_engine.reserve_daily_slot("upgrade")
    qobuz_engine.set_cooldown(60)
    assert qobuz_engine.used_today("upgrade") == 1
