"""Rockbox reads FAT32 only. The sync preview of a Rockbox card warns when the card's filesystem is
known and is not FAT; an unknown filesystem (drvfs, fuseblk) says nothing."""

import pytest

from routers import device as device_router
from services import device as device_mod
from services import wsl_mount
from services.device import card_filesystem, rockbox_fs_warning
from tests.rockbox_fakes import add_rockbox, capture_contexts

EXFAT_WARNING = "This card is formatted exFAT. Rockbox reads FAT32 cards only; use a FAT32 card for the Rockbox trial."


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(device_mod, "get_platform", lambda: "linux")

    def mounts(table):
        monkeypatch.setattr(device_mod, "_read_proc_mounts", lambda: table)
    return mounts


@pytest.mark.parametrize("fs_type, expected", [("vfat", "fat"), ("msdos", "fat"), ("exfat", "exfat"), ("ntfs3", "ntfs"),
                                               ("ntfs", "ntfs"), ("ext4", "ext4"), ("fuseblk", None)])
def test_linux_reads_the_filesystem_of_the_longest_matching_mount(linux, tmp_path, fs_type, expected):
    linux([("/", "ext4"), (str(tmp_path), fs_type), (str(tmp_path) + "-other", "vfat")])
    assert card_filesystem(str(tmp_path)) == expected


def test_linux_without_a_matching_mount_is_unknown(linux, tmp_path):
    linux([])
    assert card_filesystem(str(tmp_path)) is None


@pytest.fixture
def wsl2(monkeypatch):
    monkeypatch.setattr(device_mod, "get_platform", lambda: "wsl2")
    monkeypatch.setattr(device_mod.os.path, "realpath", lambda p: p)

    def listing(fs_by_letter):
        disks = [{"letter": letter, "fs": fs} for letter, fs in (fs_by_letter or {}).items()]
        monkeypatch.setattr(wsl_mount, "list_windows_drives",
                            lambda: None if fs_by_letter is None else {"system": "c", "disks": disks})
    return listing


@pytest.mark.parametrize("fs, expected", [("FAT32", "fat"), ("exFAT", "exfat"), ("NTFS", "ntfs"), ("", None)])
def test_wsl2_asks_windows_for_the_drive_letters_filesystem(wsl2, fs, expected):
    wsl2({"e": fs, "f": "NTFS"})
    assert card_filesystem("/mnt/e") == expected


@pytest.mark.parametrize("listing", [None, {"f": "FAT32"}])
def test_wsl2_without_an_answer_for_the_letter_is_unknown(wsl2, listing):
    wsl2(listing)
    assert card_filesystem("/mnt/e") is None


def test_the_warning_names_the_filesystem_only_when_it_is_known_and_not_fat():
    assert rockbox_fs_warning("exfat") == EXFAT_WARNING
    assert "formatted NTFS." in rockbox_fs_warning("ntfs")
    assert rockbox_fs_warning("fat") is None and rockbox_fs_warning(None) is None


async def _preview(client, card, monkeypatch, fs):
    calls = []

    def fake(path):
        calls.append(path)
        return fs

    monkeypatch.setattr(device_router, "card_filesystem", fake)
    contexts = capture_contexts(monkeypatch)
    await client.get("/api/device/diff/html", params={"device_path": str(card)})
    return contexts[-1], calls


@pytest.mark.parametrize("fs, warning", [("exfat", EXFAT_WARNING), (None, None), ("fat", None)])
@pytest.mark.asyncio
async def test_a_rockbox_preview_warns_about_a_card_that_is_not_fat(client, db, fake_device, monkeypatch, fs, warning):
    add_rockbox(fake_device)
    ctx, calls = await _preview(client, fake_device, monkeypatch, fs)
    assert ctx["fs_warning"] == warning and len(calls) == 1


@pytest.mark.asyncio
async def test_a_stock_preview_never_asks_for_the_filesystem(client, db, fake_device, monkeypatch):
    ctx, calls = await _preview(client, fake_device, monkeypatch, "exfat")
    assert ctx["fs_warning"] is None and calls == []
