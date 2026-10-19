import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from sqlalchemy import select, update

from config import settings
from models import CardCopy, SupersededFile, Track, utc_now
from services import qobuz_engine, qobuz_session, tagger
from services.qobuz_engine import QobuzSkip
from services.yt_engine import CapDeferred, DownloadFailed
from tests.qobuz_fakes import FakeClient, get_track, make_flac, qtrack, store_session

ALBUM_DIR = settings.library_dir / "Miles Davis" / "Kind of Blue"


@pytest.fixture
def client(monkeypatch):
    store_session()
    shutil.rmtree(settings.library_dir / "Miles Davis", ignore_errors=True)
    shutil.rmtree(qobuz_engine.UPGRADES_DIR, ignore_errors=True)
    fake = FakeClient(search=[qtrack()])
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: fake)
    monkeypatch.setattr(qobuz_engine, "_decoded_seconds", lambda path: FLAC(path).info.length)

    async def fake_apply(path, track_id, *, artwork_url_override=None):
        Path(path).with_suffix(".jpg").write_bytes(b"new-jpg")

    monkeypatch.setattr(tagger, "apply_full_metadata", fake_apply)
    return fake


async def lossy_track(db, name="01 - So What.m4a", folder=ALBUM_DIR, seconds=200.0, **overrides) -> int:
    """A finished lossy track (with jpg/lrc sidecars) whose upgrade the worker has just started."""
    old = make_flac(folder / name, seconds=seconds)
    old.with_suffix(".jpg").write_bytes(b"old-jpg")
    old.with_suffix(".lrc").write_text("[00:01]old lyrics")
    fields = {"title": "So What", "artist": "Miles Davis", "album": "Kind of Blue", "track_number": 1,
              "duration_ms": 200_000, "youtube_id": "yt1", "quality": "aac_256", "format": "m4a",
              "status": "done", "file_path": str(old), "engine_used": "ytmusic", "upgrade_state": "downloading"}
    fields.update(overrides)
    async with db() as session:
        track = Track(**fields)
        session.add(track)
        await session.commit()
        return track.id


async def superseded(db) -> list[SupersededFile]:
    async with db() as session:
        return list((await session.scalars(select(SupersededFile).order_by(SupersededFile.id))).all())


async def test_an_upgrade_swaps_in_the_flac_and_keeps_the_old_file(db, client):
    track_id = await lossy_track(db)

    dest = await qobuz_engine.qobuz_upgrade(track_id)

    track = await get_track(db, track_id)
    assert dest == ALBUM_DIR / "01 - So What.flac" and track.file_path == str(dest)
    assert not (ALBUM_DIR / "01 - So What.m4a").exists()
    assert dest.with_suffix(".jpg").read_bytes() == b"new-jpg"
    assert dest.with_suffix(".lrc").read_text() == "[00:01]old lyrics"
    backup = qobuz_engine.UPGRADES_DIR / str(track_id)
    assert any(p.name.endswith("01 - So What.m4a") for p in (backup / "old").iterdir())
    assert json.loads((backup / "journal.json").read_text())["phase"] == "committed"
    assert (track.engine_used, track.upgrade_state, track.synced_at) == ("qobuz", None, None)
    assert track.upgrade_backup_until > utc_now()


async def test_an_upgrade_records_what_the_next_sync_may_remove(db, client):
    track_id = await lossy_track(db)
    async with db() as session:
        session.add(CardCopy(card_id="card1", track_id=track_id, kind="audio",
                             rel_path="Miles Davis/Kind of Blue/01 - So What.m4a",
                             rel_key="miles davis/kind of blue/01 - so what.m4a", size=1, mtime=1.0, head_sha256="h"))
        await session.commit()

    await qobuz_engine.qobuz_upgrade(track_id)

    rows = await superseded(db)
    assert [(r.basis, r.card_id) for r in rows] == [("card_copy", "card1"), ("library", None)]
    assert rows[1].rel_path == "Miles Davis/Kind of Blue/01 - So What.m4a" and len(rows[1].head_sha256) == 64


async def test_a_file_outside_the_library_is_not_upgraded(db, client, tmp_path):
    track_id = await lossy_track(db, folder=tmp_path)

    with pytest.raises(DownloadFailed, match="outside the library"):
        await qobuz_engine.qobuz_upgrade(track_id)

    assert (tmp_path / "01 - So What.m4a").exists() and client.download_calls == []


async def test_a_shared_file_stays_in_place(db, client):
    track_id = await lossy_track(db)
    async with db() as session:
        session.add(Track(title="So What", artist="Miles Davis", status="done",
                          file_path=str(ALBUM_DIR / "01 - So What.m4a")))
        await session.commit()

    await qobuz_engine.qobuz_upgrade(track_id)

    assert (ALBUM_DIR / "01 - So What.m4a").exists() and await superseded(db) == []


async def test_a_case_only_rename_points_the_row_at_the_new_file(db, client):
    track_id = await lossy_track(db, name="01 - So What.FLAC", format="flac")

    dest = await qobuz_engine.qobuz_upgrade(track_id)

    assert dest.name == "01 - So What.flac" and dest.exists()
    assert (await get_track(db, track_id)).file_path == str(dest)


async def test_a_flac_of_another_length_keeps_the_current_file(db, client):
    track_id = await lossy_track(db, seconds=195.0)

    with pytest.raises(QobuzSkip, match="current file is kept") as skipped:
        await qobuz_engine.qobuz_upgrade(track_id)

    assert skipped.value.reason == "no_match"
    assert (ALBUM_DIR / "01 - So What.m4a").exists() and not (ALBUM_DIR / "01 - So What.flac").exists()


async def test_a_tagging_failure_leaves_the_library_untouched(db, client, monkeypatch):
    async def broken(path, track_id, *, artwork_url_override=None):
        raise RuntimeError("cover service down")

    monkeypatch.setattr(tagger, "apply_full_metadata", broken)
    track_id = await lossy_track(db)

    with pytest.raises(DownloadFailed, match="tagging failed"):
        await qobuz_engine.qobuz_upgrade(track_id)

    assert sorted(p.name for p in ALBUM_DIR.iterdir()) == ["01 - So What.jpg", "01 - So What.lrc", "01 - So What.m4a"]


async def test_a_row_that_changed_mid_swap_is_rolled_back_and_queued_again(db, client, monkeypatch):
    track_id = await lossy_track(db)
    real_rows = qobuz_engine._superseded_rows

    async def album_refresh_meanwhile(track, *args):
        async with db() as session:
            await session.execute(update(Track).where(Track.id == track_id).values(track_number=4))
            await session.commit()
        return await real_rows(track, *args)

    monkeypatch.setattr(qobuz_engine, "_superseded_rows", album_refresh_meanwhile)

    with pytest.raises(CapDeferred):
        await qobuz_engine.qobuz_upgrade(track_id)

    track = await get_track(db, track_id)
    assert (track.upgrade_state, track.file_path) == ("queued", str(ALBUM_DIR / "01 - So What.m4a"))
    assert sorted(p.name for p in ALBUM_DIR.iterdir()) == ["01 - So What.jpg", "01 - So What.lrc", "01 - So What.m4a"]
    assert (ALBUM_DIR / "01 - So What.jpg").read_bytes() == b"old-jpg"
    assert not (qobuz_engine.UPGRADES_DIR / str(track_id)).exists()


async def _interrupted_swap(db) -> int:
    track_id = await lossy_track(db)
    staged = make_flac(settings.download_dir / str(track_id) / "source.flac")
    old = ALBUM_DIR / "01 - So What.m4a"
    qobuz_engine._swap_in(track_id, old, staged, ALBUM_DIR / "01 - So What.flac", False, {"file_path": str(old)})
    return track_id


async def test_startup_recovery_undoes_an_interrupted_swap(db, client):
    track_id = await _interrupted_swap(db)

    assert await qobuz_engine.recover_upgrade_journals() == [track_id]

    assert sorted(p.name for p in ALBUM_DIR.iterdir()) == ["01 - So What.jpg", "01 - So What.lrc", "01 - So What.m4a"]
    assert (await get_track(db, track_id)).upgrade_state == "queued"
    shutil.rmtree(settings.download_dir / str(track_id))


async def test_startup_recovery_keeps_a_swap_whose_commit_landed(db, client):
    track_id = await _interrupted_swap(db)
    async with db() as session:
        await session.execute(update(Track).where(Track.id == track_id).values(
            file_path=str(ALBUM_DIR / "01 - So What.flac"), engine_used="qobuz", upgrade_state=None))
        await session.commit()

    await qobuz_engine.recover_upgrade_journals()

    journal = json.loads((qobuz_engine.UPGRADES_DIR / str(track_id) / "journal.json").read_text())
    assert journal["phase"] == "committed" and (ALBUM_DIR / "01 - So What.flac").exists()
    shutil.rmtree(settings.download_dir / str(track_id))


async def test_revert_puts_the_old_file_and_provenance_back(db, client):
    track_id = await lossy_track(db)
    await qobuz_engine.qobuz_upgrade(track_id)

    restored = await qobuz_engine.revert_upgrade(track_id)

    track = await get_track(db, track_id)
    assert restored == ALBUM_DIR / "01 - So What.m4a" and restored.exists()
    assert not (ALBUM_DIR / "01 - So What.flac").exists()
    assert (ALBUM_DIR / "01 - So What.jpg").read_bytes() == b"old-jpg"
    assert (track.file_path, track.engine_used, track.quality) == (str(restored), "ytmusic", "aac_256")
    assert (track.upgrade_state, track.upgrade_backup_until) == ("reverted", None)
    assert track.upgrade_eligibility(include_unmatched=False) == "reverted"
    assert [r.reason for r in await superseded(db)][-1] == "revert"


async def test_revert_without_a_backup_is_refused(db, client):
    track_id = await lossy_track(db, upgrade_state=None)

    with pytest.raises(DownloadFailed, match="no earlier file"):
        await qobuz_engine.revert_upgrade(track_id)


async def test_revert_after_the_backup_was_purged_drops_the_undo_offer(db, client):
    track_id = await lossy_track(db)
    await qobuz_engine.qobuz_upgrade(track_id)
    shutil.rmtree(qobuz_engine.UPGRADES_DIR)

    with pytest.raises(DownloadFailed, match="no earlier file"):
        await qobuz_engine.revert_upgrade(track_id)

    assert (await get_track(db, track_id)).upgrade_backup_until is None


async def test_purge_deletes_only_expired_backups(db, client):
    track_id = await lossy_track(db)
    await qobuz_engine.qobuz_upgrade(track_id)
    journal_file = qobuz_engine.UPGRADES_DIR / str(track_id) / "journal.json"

    assert qobuz_engine.purge_old_backups() == 0
    journal = json.loads(journal_file.read_text())
    journal["backup_until"] = (utc_now() - timedelta(days=1)).isoformat()
    journal_file.write_text(json.dumps(journal))

    assert qobuz_engine.purge_old_backups() == 1
    assert not journal_file.parent.exists()


async def test_queue_upgrades_skips_ineligible_and_unmatched_tracks(db, client):
    eligible = await lossy_track(db, upgrade_state=None)
    unmatched = await lossy_track(db, name="02 - Freddie.m4a", upgrade_state=None, qobuz_skip_reason="no_match",
                                  title="Freddie Freeloader", track_number=2)
    lossless = await lossy_track(db, name="03 - Blue.m4a", upgrade_state=None, is_lossless=True,
                                 title="Blue in Green", track_number=3)

    async with db() as session:
        result = await qobuz_engine.queue_upgrades(session, None, include_unmatched=False)
    assert result["queued"] == [eligible]
    assert result["skipped"] == {unmatched: "unmatched", lossless: "lossless"}

    async with db() as session:
        result = await qobuz_engine.queue_upgrades(session, [unmatched], include_unmatched=True)
    assert result["queued"] == [unmatched]


async def test_queue_upgrades_feeds_the_worker_only_what_the_cap_allows(db, client, monkeypatch):
    from worker import download_worker

    monkeypatch.setattr(settings, "qobuz_max_downloads_per_day", 2)
    monkeypatch.setitem(qobuz_engine.DAILY_SHARES, "upgrade", 1.0)
    ids = [await lossy_track(db, name=f"0{i} - T{i}.m4a", title=f"T{i}", track_number=i, upgrade_state=None)
           for i in range(1, 5)]

    async with db() as session:
        result = await qobuz_engine.queue_upgrades(session, ids, include_unmatched=False)

    assert result["queued"] == ids
    assert sorted(download_worker.queue.get_nowait()[2] for _ in range(download_worker.queue_size)) == ids[:2]


async def test_reverting_a_shared_file_upgrade_keeps_the_shared_sidecars(db, client):
    track_id = await lossy_track(db)
    async with db() as session:
        session.add(Track(title="So What", artist="Miles Davis", status="done",
                          file_path=str(ALBUM_DIR / "01 - So What.m4a")))
        await session.commit()
    await qobuz_engine.qobuz_upgrade(track_id)

    await qobuz_engine.revert_upgrade(track_id)

    assert sorted(p.name for p in ALBUM_DIR.iterdir()) == ["01 - So What.jpg", "01 - So What.lrc", "01 - So What.m4a"]
    assert (ALBUM_DIR / "01 - So What.jpg").read_bytes() == b"old-jpg"
    assert (ALBUM_DIR / "01 - So What.lrc").read_text() == "[00:01]old lyrics"


async def test_an_upgrade_without_a_new_cover_keeps_the_old_one_past_the_purge(db, client, monkeypatch):
    async def no_cover(path, track_id, *, artwork_url_override=None):
        pass

    monkeypatch.setattr(tagger, "apply_full_metadata", no_cover)
    track_id = await lossy_track(db)
    dest = await qobuz_engine.qobuz_upgrade(track_id)
    journal_file = qobuz_engine.UPGRADES_DIR / str(track_id) / "journal.json"
    journal = json.loads(journal_file.read_text())
    journal["backup_until"] = (utc_now() - timedelta(days=1)).isoformat()
    journal_file.write_text(json.dumps(journal))

    assert qobuz_engine.purge_old_backups() == 1

    assert dest.with_suffix(".jpg").read_bytes() == b"old-jpg"


async def test_an_upgrade_never_takes_the_sidecars_of_another_track_at_its_stem(db, client):
    other = make_flac(ALBUM_DIR / "01 - So What.m4a")
    other.with_suffix(".jpg").write_bytes(b"other-jpg")
    other.with_suffix(".lrc").write_text("other lyrics")
    async with db() as session:
        session.add(Track(title="So What", artist="Miles Davis", album="Kind of Blue", track_number=1,
                          status="done", file_path=str(other)))
        await session.commit()
    track_id = await lossy_track(db, name="So What (A).m4a")

    with pytest.raises(DownloadFailed, match="current file is kept"):
        await qobuz_engine.qobuz_upgrade(track_id)

    assert other.with_suffix(".jpg").read_bytes() == b"other-jpg"
    assert other.with_suffix(".lrc").read_text() == "other lyrics"
    assert not (ALBUM_DIR / "01 - So What.flac").exists()
    assert (await get_track(db, track_id)).file_path == str(ALBUM_DIR / "So What (A).m4a")
