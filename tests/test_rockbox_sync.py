"""A card with /.rockbox is a Rockbox card: playlists go to /Playlists with absolute entries, the
preview and the result remind to refresh Rockbox's database. Stock cards keep today's bytes."""

import json
import os

import pytest

from routers import device as device_router
from services import device as device_mod
from services.device import CARD_PROFILE_ROCKBOX, CARD_PROFILE_STOCK, ROCKBOX_DB_REMINDER, card_profile
from services.playlist import MANIFEST_NAME, generate_m3u, record_generated_playlists
from tests.rockbox_fakes import add_playlist, add_rockbox, capture_contexts, seed_track, sync

ROCKBOX_PLAYLIST = b"#EXTM3U\n/Artist/Album/01 - Song.mp3\n"
STOCK_PLAYLIST = b"\xef\xbb\xbfArtist/Album/01 - Song.mp3\r\n"


def _manifest(card) -> list[str]:
    return json.loads((card / MANIFEST_NAME).read_text(encoding="utf-8"))["files"]


def test_a_rockbox_folder_makes_a_rockbox_card(tmp_path):
    add_rockbox(tmp_path)
    assert card_profile(tmp_path) == CARD_PROFILE_ROCKBOX == "rockbox"


def test_the_rockbox_folder_is_found_in_any_case(tmp_path):
    (tmp_path / ".ROCKBOX").mkdir()
    assert card_profile(tmp_path) == "rockbox"


@pytest.mark.parametrize("make", [lambda root: (root / ".rockbox").write_text("a file"), lambda root: None])
def test_without_a_rockbox_folder_the_card_is_stock(tmp_path, make):
    make(tmp_path)
    assert card_profile(tmp_path) == CARD_PROFILE_STOCK == "stock"


def test_a_missing_card_counts_as_stock(tmp_path):
    assert card_profile(tmp_path / "gone") == "stock"


def test_detecting_the_profile_changes_nothing_on_the_card(tmp_path):
    add_rockbox(tmp_path)
    before = (sorted(os.listdir(tmp_path)), os.stat(tmp_path).st_mtime_ns)

    card_profile(tmp_path)

    assert (sorted(os.listdir(tmp_path)), os.stat(tmp_path).st_mtime_ns) == before


def test_detected_volumes_carry_their_profile(tmp_path):
    add_rockbox(tmp_path)
    assert device_mod._classify(str(tmp_path), "H2", "H", True)["profile"] == "rockbox"
    assert device_mod._classify(str(tmp_path / ".rockbox"), "H2", "H", True)["profile"] == "stock"


def test_a_card_holding_only_rockbox_has_no_music(tmp_path):
    add_rockbox(tmp_path)
    assert device_mod._classify(str(tmp_path), "H2", "H", False)["has_music_files"] is False


def test_a_rockbox_playlist_lives_in_playlists_with_absolute_entries(tmp_path):
    path = generate_m3u("Road: Trip?", [{"rel_path": "Artist/Album/01 - Song.mp3"}], tmp_path, profile="rockbox")
    assert path == tmp_path / "Playlists" / "Road Trip.m3u8"
    assert path.read_bytes() == ROCKBOX_PLAYLIST


@pytest.mark.asyncio
async def test_a_rockbox_sync_writes_playlists_to_the_playlists_folder(client, db, fake_device, tmp_path):
    add_rockbox(fake_device)
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])

    done = await sync(client, fake_device)

    assert (fake_device / "Playlists" / "Mix.m3u8").read_bytes() == ROCKBOX_PLAYLIST
    assert _manifest(fake_device) == ["Playlists/Mix.m3u8"]
    assert not list(fake_device.glob("*.m3u8"))
    assert (done["profile"], done["reminder"], done["playlists"]) == ("rockbox", ROCKBOX_DB_REMINDER, 1)


@pytest.mark.asyncio
async def test_a_stock_sync_keeps_todays_playlist_bytes_and_writes_nothing_for_rockbox(client, db, fake_device,
                                                                                       tmp_path):
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])

    done = await sync(client, fake_device)

    assert (fake_device / "Mix.m3u8").read_bytes() == STOCK_PLAYLIST
    assert (fake_device / MANIFEST_NAME).read_text(encoding="utf-8") == json.dumps(
        {"version": 1, "files": ["Mix.m3u8"]}, indent=2)
    assert not (fake_device / "Playlists").exists() and not (fake_device / ".leasure-covers.json").exists()
    assert not list(fake_device.rglob("cover.jpg"))
    assert (done["profile"], done["reminder"], done["covers_written"], done["covers_removed"]) == ("stock", None, 0, 0)


@pytest.mark.asyncio
async def test_a_card_that_becomes_rockbox_moves_leasures_playlists_and_keeps_the_users(client, db, fake_device,
                                                                                         tmp_path):
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])
    (fake_device / "My Mix.m3u").write_text("user list")
    await sync(client, fake_device)
    add_rockbox(fake_device)

    await sync(client, fake_device)

    assert not (fake_device / "Mix.m3u8").exists()
    assert (fake_device / "Playlists" / "Mix.m3u8").read_bytes() == ROCKBOX_PLAYLIST
    assert (fake_device / "My Mix.m3u").read_text() == "user list"
    assert _manifest(fake_device) == ["Playlists/Mix.m3u8"]


@pytest.mark.asyncio
async def test_a_card_back_on_stock_gets_its_playlists_at_the_root_again(client, db, fake_device, tmp_path):
    import shutil

    add_rockbox(fake_device)
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])
    await sync(client, fake_device)
    (fake_device / "Playlists" / "Mine.m3u8").write_text("user list")
    shutil.rmtree(fake_device / ".rockbox")

    await sync(client, fake_device)

    assert (fake_device / "Mix.m3u8").read_bytes() == STOCK_PLAYLIST
    assert not (fake_device / "Playlists" / "Mix.m3u8").exists()
    assert (fake_device / "Playlists" / "Mine.m3u8").read_text() == "user list"
    assert _manifest(fake_device) == ["Mix.m3u8"]


@pytest.mark.asyncio
async def test_a_playlists_file_in_the_way_is_reported_and_left_alone(client, db, fake_device, tmp_path):
    add_rockbox(fake_device)
    (fake_device / "Playlists").write_text("not a folder")
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])

    done = await sync(client, fake_device)

    assert (fake_device / "Playlists").read_text() == "not a folder"
    assert "Rockbox playlists not written: /Playlists is not a folder" in done["errors"]
    assert done["copied"] == 1


def test_the_sweep_keeps_a_moved_playlist_until_its_new_copy_exists(tmp_path):
    from services.playlist import sweep_orphan_playlists

    old = generate_m3u("Mix", [{"rel_path": "A/B/01 - C.mp3"}], tmp_path)
    record_generated_playlists(tmp_path, [old])

    assert sweep_orphan_playlists(tmp_path, {"Mix"}, profile="rockbox") == []
    assert old.exists()


@pytest.mark.asyncio
async def test_the_preview_of_a_rockbox_card_names_it_and_ends_with_the_database_reminder(client, db, fake_device,
                                                                                         monkeypatch):
    add_rockbox(fake_device)
    monkeypatch.setattr(device_router, "card_filesystem", lambda path: "fat")
    contexts = capture_contexts(monkeypatch)

    await client.get("/api/device/diff/html", params={"device_path": str(fake_device)})

    ctx = contexts[-1]
    assert (ctx["profile"], ctx["db_reminder"], ctx["playlist_dir"], ctx["fs_warning"]) == (
        "rockbox", ROCKBOX_DB_REMINDER, "/Playlists", None)


@pytest.mark.asyncio
async def test_the_preview_of_a_stock_card_has_no_reminder(client, db, fake_device, monkeypatch):
    contexts = capture_contexts(monkeypatch)

    await client.get("/api/device/diff/html", params={"device_path": str(fake_device)})

    ctx = contexts[-1]
    assert (ctx["profile"], ctx["db_reminder"], ctx["playlist_dir"], ctx["fs_warning"]) == ("stock", None, "/", None)


@pytest.mark.asyncio
async def test_the_legacy_sync_result_carries_the_reminder_on_rockbox(client, db, fake_device, tmp_path, monkeypatch):
    add_rockbox(fake_device)
    await seed_track(db, tmp_path)
    contexts = capture_contexts(monkeypatch)

    resp = await client.post("/api/device/sync", data={"device_path": str(fake_device)})

    assert resp.status_code == 200
    assert contexts[-1]["reminder"] == ROCKBOX_DB_REMINDER and contexts[-1]["synced"] == 1


@pytest.mark.asyncio
async def test_an_empty_rockbox_sync_still_reminds(client, db, fake_device):
    add_rockbox(fake_device)

    done = await sync(client, fake_device)

    assert (done["total"], done["profile"], done["reminder"]) == (0, "rockbox", ROCKBOX_DB_REMINDER)


@pytest.mark.asyncio
async def test_a_file_too_large_for_fat32_is_skipped_on_a_rockbox_card(client, db, fake_device, tmp_path, monkeypatch):
    add_rockbox(fake_device)
    monkeypatch.setattr(device_router, "_MAX_CARD_FILE_BYTES", 100)
    # Unreadable filesystem: a Rockbox card is FAT32 all the same.
    monkeypatch.setattr(device_router, "card_filesystem", lambda path: None)
    await seed_track(db, tmp_path, body=b"x" * 200)

    done = await sync(client, fake_device)

    assert done["copied"] == 0 and done["errors"] == ["Song: too large for the card"]
    assert not (fake_device / "Artist").exists()


async def _delete_playlists(db) -> None:
    from sqlalchemy import delete

    from models import Playlist, PlaylistTrack

    async with db() as session:
        await session.execute(delete(PlaylistTrack))
        await session.execute(delete(Playlist))
        await session.commit()


@pytest.mark.asyncio
async def test_a_users_rockbox_playlist_of_the_same_name_is_never_overwritten_or_swept(client, db, fake_device,
                                                                                         tmp_path):
    add_rockbox(fake_device)
    (fake_device / "Playlists").mkdir()
    (fake_device / "Playlists" / "Road Trip.m3u8").write_bytes(b"/Elsewhere/song.mp3\n")
    await add_playlist(db, "Road Trip", [await seed_track(db, tmp_path)])

    done = await sync(client, fake_device)
    await _delete_playlists(db)
    await sync(client, fake_device)

    assert (fake_device / "Playlists" / "Road Trip.m3u8").read_bytes() == b"/Elsewhere/song.mp3\n"
    assert not (fake_device / MANIFEST_NAME).exists() or _manifest(fake_device) == []
    assert done["playlists"] == 0
    assert any("/Playlists/Road Trip.m3u8 is not one of Leasure's playlists" in e for e in done["errors"])


@pytest.mark.asyncio
async def test_leasures_own_rockbox_playlist_is_rewritten_on_the_next_sync(client, db, fake_device, tmp_path):
    add_rockbox(fake_device)
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])
    await sync(client, fake_device)
    (fake_device / "Playlists" / "Mix.m3u8").write_bytes(b"stale")

    done = await sync(client, fake_device)

    assert (fake_device / "Playlists" / "Mix.m3u8").read_bytes() == ROCKBOX_PLAYLIST
    assert (done["playlists"], done["errors"]) == (1, [])


@pytest.mark.asyncio
async def test_a_root_playlist_is_kept_when_the_playlists_copy_is_the_users(client, db, fake_device, tmp_path):
    await add_playlist(db, "Mix", [await seed_track(db, tmp_path)])
    await sync(client, fake_device)
    add_rockbox(fake_device)
    (fake_device / "Playlists").mkdir()
    (fake_device / "Playlists" / "Mix.m3u8").write_bytes(b"user list")

    await sync(client, fake_device)

    assert (fake_device / "Mix.m3u8").read_bytes() == STOCK_PLAYLIST
    assert (fake_device / "Playlists" / "Mix.m3u8").read_bytes() == b"user list"
