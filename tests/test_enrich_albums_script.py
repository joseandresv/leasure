import os
import time

import pytest
from sqlalchemy import create_engine, insert, select

import db as db_module
from models import AlbumInfo, Track, utc_now
from scripts import enrich_albums
from services import album_info, local_albums
from tests.album_fakes import seed


@pytest.fixture
def lock():
    path = album_info.lock_path()
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def lookups(monkeypatch):
    keys: list[str] = []

    async def fake_lookup(key, pin=None, sources=album_info.SOURCES):
        keys.append(key)
        return "not_found"

    monkeypatch.setattr(album_info, "lookup", fake_lookup)
    return keys


def tracks_only_db(path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    Track.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(insert(Track).values(title="Mr. Brightside", artist="The Killers", album="Hot Fuss",
                                          status="done", quality="aac_256", format="m4a", source="spotify",
                                          is_lossless=False))
    engine.dispose()


async def test_the_dry_run_reads_a_database_without_album_info_and_changes_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "leasure.db"
    tracks_only_db(path)
    before = path.read_bytes()

    async def refuse():
        raise AssertionError("init_db called")

    monkeypatch.setattr(db_module, "init_db", refuse)
    monkeypatch.setattr(db_module, "DB_PATH", path)

    assert await enrich_albums.run(enrich_albums.parse_args([])) == 0

    out = capsys.readouterr().out
    assert "The Killers — Hot Fuss: due" in out and "Dry run" in out
    assert path.read_bytes() == before


async def test_the_dry_run_reports_stored_states(db, capsys):
    await seed(db)
    await seed(db, title="Somebody Told Me", album="Sam's Town")
    async with db() as session:
        session.add(AlbumInfo(album_key=local_albums.key_of("Sam's Town", "The Killers", "The Killers"),
                              key_version="2", key_album_raw="Sam's Town", key_artist_raw="The Killers",
                              status="not_found", fetched_at=utc_now(), retry_at=utc_now().replace(year=2099)))
        await session.commit()

    assert await enrich_albums.run(enrich_albums.parse_args([])) == 0

    out = capsys.readouterr().out
    assert "Hot Fuss: due" in out and "Sam's Town: not_found (retry 2099-" in out


async def test_apply_looks_up_at_most_the_limit(db, lookups, lock, capsys):
    for album in ("A", "B", "C"):
        await seed(db, album=album)

    assert await enrich_albums.run(enrich_albums.parse_args(["--apply", "--limit", "2"])) == 0

    assert lookups == [local_albums.key_of(a, "The Killers", "The Killers") for a in ("A", "B")]
    assert "[2/2] The Killers — B  -> not found" in capsys.readouterr().out
    assert not lock.exists()


async def test_apply_refuses_while_another_run_holds_the_lock(db, lookups, lock, capsys):
    await seed(db)
    lock.write_text("{}", encoding="utf-8")

    assert await enrich_albums.run(enrich_albums.parse_args(["--apply"])) == 1

    assert lookups == [] and "Another enrich run holds" in capsys.readouterr().err
    assert lock.exists()


async def test_apply_replaces_a_stale_lock(db, lookups, lock):
    await seed(db)
    lock.write_text("{}", encoding="utf-8")
    old = time.time() - album_info.LOCK_STALE_S - 5
    os.utime(lock, (old, old))

    assert await enrich_albums.run(enrich_albums.parse_args(["--apply"])) == 0
    assert len(lookups) == 1


async def test_apply_with_every_lookup_failing_exits_1(db, monkeypatch, lock):
    await seed(db)

    async def error(key, **kwargs):
        return "error"

    monkeypatch.setattr(album_info, "lookup", error)

    assert await enrich_albums.run(enrich_albums.parse_args(["--apply"])) == 1


async def test_prune_drops_info_of_albums_no_longer_in_the_library(db, lookups, lock):
    await seed(db)
    async with db() as session:
        session.add(AlbumInfo(album_key="a-" + "9" * 20, key_version="2", key_album_raw="Gone",
                              key_artist_raw="X", status="not_found", fetched_at=utc_now()))
        await session.commit()

    await enrich_albums.run(enrich_albums.parse_args(["--apply", "--prune"]))

    async with db() as session:
        assert (await session.scalars(select(AlbumInfo.album_key))).all() == []


def test_the_guard_refuses_a_database_outside_the_test_temp_dir():
    with pytest.raises(SystemExit) as stop:
        enrich_albums.guard_test_database(enrich_albums.REPO_ROOT / "data" / "leasure.db")

    assert stop.value.code == 2


def test_prune_needs_apply():
    with pytest.raises(SystemExit):
        enrich_albums.parse_args(["--prune"])
