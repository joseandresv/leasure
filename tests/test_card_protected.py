"""Rockbox's /.rockbox and the OS's system folders on a card are never listed, imported, written,
swept or deleted by any code path, whatever case their names use."""

import hashlib
import json
import os

import pytest

from models import SupersededFile, Track
from services import card_import
from services.device import artist_folder, is_protected_card_path, is_protected_on_card, remove_empty_card_dirs
from services.playlist import MANIFEST_NAME, sweep_orphan_playlists
from tests.rockbox_fakes import add_rockbox, capture_contexts, seed_track, sync, tree_digest


@pytest.mark.parametrize("rel", [".rockbox", "/.rockbox/x", ".ROCKBOX\\a", "./.rockbox", ".Rockbox/codecs/mpa.codec",
                                 "System Volume Information/x", "system volume information", "$recycle.bin",
                                 "$RECYCLE.BIN/S-1-5/x.mp3", ".Trash-1000/x", ".Trashes/x", ".Spotlight-V100",
                                 ".fseventsd/x", "../x", "Artist/../../x", "", "/"])
def test_protected_paths(rel):
    assert is_protected_card_path(rel)


@pytest.mark.parametrize("rel", ["Artist/.rockbox/x", "Rockbox/x", "Playlists/x.m3u8", "Artist/Album/01 - Song.mp3",
                                 "Mix.m3u8", ".leasure-playlists.json"])
def test_ordinary_paths_are_not_protected(rel):
    assert not is_protected_card_path(rel)


def test_a_folder_that_links_into_rockbox_is_protected(tmp_path):
    add_rockbox(tmp_path)
    os.symlink(tmp_path / ".rockbox", tmp_path / "Artist")

    assert is_protected_on_card(tmp_path, tmp_path / "Artist" / "x.mp3")
    assert is_protected_on_card(tmp_path, tmp_path.parent / "elsewhere.mp3")
    assert is_protected_on_card(tmp_path, tmp_path)
    assert not is_protected_on_card(tmp_path, tmp_path / "Other" / "x.mp3")


@pytest.mark.parametrize("name", ["System Volume Information", "$RECYCLE.BIN", "system volume information"])
def test_an_artist_named_like_a_system_folder_gets_its_own_folder(name):
    assert artist_folder(name) == f"_{name}"
    assert not is_protected_card_path(artist_folder(name))


def test_empty_folders_inside_rockbox_are_never_removed(tmp_path):
    empty = add_rockbox(tmp_path) / "empty" / "deeper"
    empty.mkdir(parents=True)

    remove_empty_card_dirs(tmp_path, empty)

    assert empty.is_dir()


@pytest.mark.asyncio
async def test_a_superseded_record_pointing_into_rockbox_is_never_deleted(client, db, fake_device, tmp_path):
    add_rockbox(fake_device)
    track = await seed_track(db, tmp_path)
    await sync(client, fake_device)
    victim = fake_device / ".rockbox" / "codecs" / "x.mp3"
    victim.write_bytes(b"\xff\xfb" * 100)
    st = victim.stat()
    async with db() as session:
        session.add(SupersededFile(track_id=track.id, card_id=None, basis="library", kind="audio",
                                   rel_path=".rockbox/codecs/x.mp3", size=st.st_size, mtime=st.st_mtime,
                                   head_sha256=hashlib.sha256(victim.read_bytes()).hexdigest()))
        await session.commit()
    before = tree_digest(fake_device / ".rockbox")

    done = await sync(client, fake_device)

    assert done["removed"] == 0 and victim.exists()
    assert tree_digest(fake_device / ".rockbox") == before


def test_the_playlist_sweep_never_deletes_a_manifest_entry_in_a_protected_or_outside_path(tmp_path):
    card = tmp_path / "card"
    add_rockbox(card)
    add_rockbox(card, ".ROCKBOX")
    (card / ".ROCKBOX" / "y.m3u8").write_text("x")
    (tmp_path / "evil.m3u8").write_text("x")
    (card / "Playlists").mkdir()
    entries = [".rockbox/x.m3u8", "../evil.m3u8", "Playlists/../.rockbox/x.m3u8", ".ROCKBOX/y.m3u8", "/evil.m3u8",
               "Playlists\\..\\.rockbox\\x.m3u8"]
    (card / MANIFEST_NAME).write_text(json.dumps({"version": 1, "files": entries}))
    before = tree_digest(tmp_path)

    removed = sweep_orphan_playlists(card, keep_names=set())

    assert removed == []
    assert tree_digest(tmp_path) == before


@pytest.mark.parametrize("folder", [".rockbox", ".ROCKBOX", "System Volume Information"])
def test_cleaning_stale_copies_never_removes_a_protected_file(tmp_path, folder):
    card, library = tmp_path / "card", tmp_path / "library"
    add_rockbox(card, folder)
    (card / folder / "a").mkdir(parents=True, exist_ok=True)
    (card / folder / "a" / "b.mp3").write_bytes(b"x" * 10)
    (card / "Artist" / "Album").mkdir(parents=True)
    (card / "Artist" / "Album" / "01 - Song.mp3").write_bytes(b"x" * 10)
    library.mkdir()
    (library / "Song.mp3").write_bytes(b"x" * 10)
    row = Track(title="Song", artist="Artist", album="Album", track_number=1, status="done",
                file_path=str(library / "Song.mp3"))
    before = tree_digest(card / folder)

    result = card_import.remove_stale_copies(
        card, [{"card": f"{folder}/a/b.mp3", "replaced_by": "Artist/Album/01 - Song.mp3"}], [row], apply=True)

    assert result["removed"] == [] and result["kept"][0]["reason"] == "in a protected system folder on the card"
    assert tree_digest(card / folder) == before


def test_removing_with_empty_parents_refuses_a_protected_path(tmp_path):
    victim = add_rockbox(tmp_path) / "x.mp3"

    with pytest.raises(OSError):
        card_import._remove_with_empty_parents(tmp_path, [victim])

    assert victim.exists()


def test_the_clean_card_script_keeps_a_report_entry_inside_rockbox(fake_device, tmp_path, monkeypatch, capsys):
    from scripts import clean_card

    add_rockbox(fake_device)
    (fake_device / "Artist" / "Album").mkdir(parents=True)
    (fake_device / "Artist" / "Album" / "01 - Song.mp3").write_bytes(b"x" * 10)
    library = tmp_path / "library.mp3"
    library.write_bytes(b"x" * 10)
    rows = [Track(title="Song", artist="Artist", album="Album", track_number=1, status="done", file_path=str(library))]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def scalars(self, _stmt):
            return type("Rows", (), {"all": lambda self: rows})()

    monkeypatch.setattr(card_import, "async_session", Session)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"stale_card_copies": [
        {"card": ".rockbox/x.mp3", "replaced_by": "Artist/Album/01 - Song.mp3"}]}))
    before = tree_digest(fake_device / ".rockbox")

    assert clean_card.main(["--card", str(fake_device), "--report", str(report), "--apply"]) == 0

    assert tree_digest(fake_device / ".rockbox") == before
    assert "removed 0 stale card files, kept 1" in capsys.readouterr().out


def test_a_card_scan_skips_protected_folders_in_any_case(tmp_path):
    for rel in (".rockbox/x.mp3", ".Rockbox/y.mp3", "System Volume Information/z.mp3",
                "system volume information/z.mp3", "$Recycle.Bin/w.mp3", "Artist/Album/01 - Song.mp3"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"not audio")

    found, errors = card_import.scan_card(tmp_path)

    assert [f.rel for f in found] + [e["card"] for e in errors] == ["Artist/Album/01 - Song.mp3"]


@pytest.mark.asyncio
async def test_an_import_reports_nothing_from_rockbox(db, tmp_path, monkeypatch, isolated_paths):
    from config import settings

    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    isolated_paths()
    card = tmp_path / "card"
    add_rockbox(card)

    report = await card_import.import_card(card, tmp_path / "report" / "report.json", dry_run=True, lookup=False)

    assert ".rockbox" not in json.dumps(report.to_dict()).casefold()


def test_card_playlists_ignore_playlists_inside_rockbox(tmp_path):
    card = tmp_path / "card"
    add_rockbox(card)
    (card / "Playlists").mkdir()
    (card / "Playlists" / "Mix.m3u8").write_text("/Artist/Album/01 - Song.mp3\n")

    found = card_import.card_playlists(card, tmp_path / "backup")

    assert [(name, entries) for name, entries, _ in found] == [("Mix", ["Artist/Album/01 - Song.mp3"])]


def test_the_file_browser_leaves_rockbox_out(tmp_path):
    from routers.device import _scan_device_tree

    add_rockbox(tmp_path)
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "01 - Song.mp3").write_bytes(b"x")

    listing = _scan_device_tree(tmp_path)

    assert list(listing["artists"]) == ["Artist"] and listing["total_tracks"] == 1
    assert listing["playlists"] == []
    assert listing["protected"] == [".rockbox", "System Volume Information"]


@pytest.mark.asyncio
async def test_the_files_page_reports_what_it_left_alone(client, fake_device, monkeypatch):
    add_rockbox(fake_device)
    contexts = capture_contexts(monkeypatch)

    resp = await client.get("/api/device/files/html", params={"device_path": str(fake_device)})

    assert resp.status_code == 200
    assert contexts[-1]["protected"] == [".rockbox"] and ".rockbox" not in contexts[-1]["artists"]


@pytest.mark.asyncio
async def test_the_sync_preview_does_not_list_rockbox_files_as_on_device_only(client, db, fake_device, monkeypatch):
    add_rockbox(fake_device)
    (fake_device / "Other" / "Album").mkdir(parents=True)
    (fake_device / "Other" / "Album" / "01 - Mine.mp3").write_bytes(b"x")
    contexts = capture_contexts(monkeypatch)

    await client.get("/api/device/diff/html", params={"device_path": str(fake_device)})

    assert contexts[-1]["on_device_only"] == ["Other/Album/01 - Mine.mp3"]


def test_a_copy_never_writes_into_a_protected_path(tmp_path, monkeypatch):
    from routers import device

    add_rockbox(tmp_path / "card")
    src = tmp_path / "song.mp3"
    src.write_bytes(b"new bytes")
    monkeypatch.setattr(device, "_device_rel_path", lambda track, ext: f".rockbox/x.{ext}")
    before = tree_digest(tmp_path / "card")

    with pytest.raises(ValueError):
        device._copy_track(src, tmp_path / "card", Track(id=1, title="Song", artist="A", album="B"))

    assert tree_digest(tmp_path / "card") == before


@pytest.mark.asyncio
async def test_a_full_sync_leaves_rockbox_byte_identical(client, db, fake_device, tmp_path):
    add_rockbox(fake_device)
    await seed_track(db, tmp_path)
    before = tree_digest(fake_device / ".rockbox")

    done = await sync(client, fake_device)

    assert done["copied"] == 1
    assert tree_digest(fake_device / ".rockbox") == before
