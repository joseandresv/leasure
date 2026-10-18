import os

import pytest
from sqlalchemy import select

from config import settings
from models import Track
from scripts import move_library


@pytest.fixture
def library(tmp_path, monkeypatch, isolated_paths):
    root = tmp_path / "library"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 - Song.mp3").write_bytes(b"audio")
    (album / "01 - Song.jpg").write_bytes(b"jpg")
    os.utime(album / "01 - Song.mp3", (1_700_000_000, 1_700_000_000))
    monkeypatch.setattr(settings, "library_dir", root)
    isolated_paths()
    return root


async def _add(db, file_path: str) -> int:
    async with db() as session:
        track = Track(title="Song", artist="Artist", status="done", file_path=file_path)
        session.add(track)
        await session.commit()
        return track.id


async def _paths(db) -> list[str]:
    async with db() as session:
        return list((await session.scalars(select(Track.file_path).order_by(Track.id))).all())


@pytest.mark.asyncio
async def test_move_relocates_files_and_rewrites_track_paths(db, library, tmp_path):
    dest = tmp_path / "Music" / "Leasure"
    await _add(db, str(library / "Artist" / "Album" / "01 - Song.mp3"))
    await _add(db, "/elsewhere/kept.mp3")

    files, rows = await move_library.move_library(dest, dry_run=False)

    moved = dest / "Artist" / "Album" / "01 - Song.mp3"
    assert moved.read_bytes() == b"audio" and (dest / "Artist" / "Album" / "01 - Song.jpg").exists()
    assert abs(moved.stat().st_mtime - 1_700_000_000) <= 2
    assert not (library / "Artist").exists() and library.exists()
    assert await _paths(db) == [str(moved), "/elsewhere/kept.mp3"]
    assert (files["moved"], rows["rewritten"], rows["outside"]) == (2, 1, 1)


@pytest.mark.asyncio
async def test_a_relative_track_path_is_rewritten_too(db, library, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "library_dir", library.relative_to(tmp_path))
    await _add(db, "library/Artist/Album/01 - Song.mp3")

    await move_library.move_library(tmp_path / "new", dry_run=False)

    assert await _paths(db) == [str(tmp_path / "new" / "Artist" / "Album" / "01 - Song.mp3")]


@pytest.mark.asyncio
async def test_a_rerun_after_an_interruption_finishes_the_move(db, library, tmp_path):
    dest = tmp_path / "new"
    copied = dest / "Artist" / "Album" / "01 - Song.mp3"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(b"audio")
    await _add(db, str(library / "Artist" / "Album" / "01 - Song.mp3"))

    files, rows = await move_library.move_library(dest, dry_run=False)

    assert (files["already_there"], files["moved"], rows["rewritten"]) == (1, 1, 1)
    assert not (library / "Artist" / "Album" / "01 - Song.mp3").exists()


@pytest.mark.asyncio
async def test_a_different_file_at_the_destination_is_never_overwritten(db, library, tmp_path):
    dest = tmp_path / "new"
    clash = dest / "Artist" / "Album" / "01 - Song.mp3"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"other")

    files, _ = await move_library.move_library(dest, dry_run=False)

    assert clash.read_bytes() == b"other"
    assert (library / "Artist" / "Album" / "01 - Song.mp3").read_bytes() == b"audio"
    assert len(files["conflicts"]) == 1


@pytest.mark.asyncio
async def test_dry_run_moves_nothing(db, library, tmp_path):
    original = str(library / "Artist" / "Album" / "01 - Song.mp3")
    await _add(db, original)

    files, rows = await move_library.move_library(tmp_path / "new", dry_run=True)

    assert (files["moved"], rows["rewritten"]) == (2, 1)
    assert not (tmp_path / "new").exists() and await _paths(db) == [original]


def test_the_move_is_refused_while_the_server_is_listening(library, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(move_library, "server_listening", lambda host, port: True)

    assert move_library.main([str(tmp_path / "new")]) == 2
    assert not (tmp_path / "new").exists() and "Stop it first" in capsys.readouterr().out


def test_a_destination_inside_the_library_is_refused(library):
    assert move_library.main([str(library / "sub")]) == 2


def test_server_listening_sees_a_bound_port():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert move_library.server_listening("127.0.0.1", server.getsockname()[1])


@pytest.mark.asyncio
async def test_a_row_on_a_conflict_path_keeps_pointing_at_its_own_file(db, library, tmp_path):
    dest = tmp_path / "new"
    clash = dest / "Artist" / "Album" / "01 - Song.mp3"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"other")
    original = str(library / "Artist" / "Album" / "01 - Song.mp3")
    await _add(db, original)

    _, rows = await move_library.move_library(dest, dry_run=False)

    assert await _paths(db) == [original] and rows["not_moved"] == [original]


def test_the_env_line_is_withheld_while_files_stay_behind(library, tmp_path, monkeypatch, capsys):
    async def conflicted(dest_root, dry_run):
        return ({"moved": 0, "already_there": 0, "conflicts": ["x"], "errors": []},
                {"rewritten": 0, "outside": 0, "missing": [], "not_moved": ["x"]})

    monkeypatch.setattr(move_library, "server_listening", lambda host, port: False)
    monkeypatch.setattr(move_library, "move_library", conflicted)

    assert move_library.main([str(tmp_path / "new")]) == 1
    assert "LIBRARY_DIR=" not in capsys.readouterr().out
