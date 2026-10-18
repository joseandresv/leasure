"""WSL2 card mount/eject. Every subprocess goes through wsl_mount._run, which is faked
here, so nothing touches the host's real drives (the owner's card may be at /mnt/e)."""
import errno
import json
import subprocess
import sys
import threading
import time

import pytest

from services import wsl_mount

WSL = ["/fake/wsl.exe", "-d", "Ubuntu", "-u", "root", "-e"]
MOUNT_E = WSL + ["/bin/mount", "-t", "drvfs", "E:", "/mnt/e", "-o", "uid=1000,gid=1000"]


def _listing(system="c", extra=()):
    disks = [
        {"letter": "c", "label": "OS", "fs": "NTFS", "total_bytes": 2 * 10**12, "free_bytes": 10**12,
         "has_media": True, "removable": False, "is_system": True},
        {"letter": "d", "label": "D:", "fs": "", "total_bytes": None, "free_bytes": None,
         "has_media": False, "removable": True, "is_system": False},
        {"letter": "e", "label": "E:", "fs": "exFAT", "total_bytes": 124_656_812_032, "free_bytes": 10**11,
         "has_media": True, "removable": True, "is_system": False},
        {"letter": "f", "label": "F:", "fs": "", "total_bytes": None, "free_bytes": None,
         "has_media": False, "removable": True, "is_system": False},
        *extra,
    ]
    return {"system": system, "disks": disks}


class FakeRunner:
    """Records each argv; mount/umount success updates the fake mount table. Like drvfs,
    mounts stack: `stacked[letter]` umounts are needed before the letter leaves the table."""

    def __init__(self, mounts: dict):
        self.calls: list[list[str]] = []
        self.mounts = mounts
        self.stacked: dict[str, int] = {}
        self.replies: dict[str, tuple] = {}
        self.unstartable: str | None = None

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[0] == self.unstartable:
            raise OSError(errno.ENOEXEC, "Exec format error")
        binary = next((a for a in argv if a.startswith("/bin/")), argv[0])
        rc, stderr, *stdout = self.replies.get(binary, (0, ""))
        if rc == 0 and binary == "/bin/mount":
            letter = argv[argv.index("/bin/mount") + 4][-1]
            self.mounts[letter] = "ok"
            self.stacked[letter] = self.stacked.get(letter, 0) + 1
        if rc == 0 and binary == "/bin/umount":
            letter = argv[-1][-1]
            self.stacked[letter] = self.stacked.get(letter, 1) - 1
            if self.stacked[letter] <= 0:
                self.mounts.pop(letter, None)
        return subprocess.CompletedProcess(argv, rc, "".join(stdout), stderr)


@pytest.fixture
def wsl(monkeypatch):
    from routers import device as device_router
    from services import device as device_service
    from services import platform as platform_service

    for module in (platform_service, device_router, device_service):
        monkeypatch.setattr(module, "get_platform", lambda: "wsl2")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(wsl_mount, "interop_enabled", lambda: True)
    monkeypatch.setattr(wsl_mount, "_win_exe", lambda name, fallback: "/fake/wsl.exe")
    monkeypatch.setattr(wsl_mount.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    monkeypatch.setattr(wsl_mount.os, "getuid", lambda: 1000)
    monkeypatch.setattr(wsl_mount.os, "getgid", lambda: 1000)
    monkeypatch.setattr(wsl_mount.os.path, "lexists", lambda path: False)
    mounts: dict[str, str] = {}
    monkeypatch.setattr(wsl_mount, "mounted_letters", lambda: dict(mounts))
    listing = _listing()
    monkeypatch.setattr(wsl_mount, "list_windows_drives", lambda: listing)
    runner = FakeRunner(mounts)
    monkeypatch.setattr(wsl_mount, "_run", runner)
    monkeypatch.setattr(device_router, "detect_devices", lambda: [])
    return runner


async def test_mount_runs_mkdir_then_drvfs_mount_as_root_through_wsl(client, wsl):
    resp = await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls == [WSL + ["/bin/mkdir", "-p", "/mnt/e"], MOUNT_E]
    assert resp.headers["HX-Trigger"] == "leasure:drives-changed"
    assert "mounted at /mnt/e" in resp.text


@pytest.mark.parametrize("letter", ["c", "C", "1", "ee", "", "e;rm", "z", "d"])
async def test_mount_refuses_bad_or_unusable_letters_without_running_anything(client, wsl, letter):
    resp = await client.post("/api/device/mount", data={"letter": letter})

    assert wsl.calls == []
    assert "<script" not in resp.text
    assert resp.headers["HX-Trigger"] == "leasure:drives-changed"
    assert "mounted at" not in resp.text


async def test_mount_reports_an_empty_card_slot(client, wsl):
    resp = await client.post("/api/device/mount", data={"letter": "d"})

    assert "No card in D:" in resp.text


async def test_mount_refuses_the_system_letter_reported_by_windows(client, wsl, monkeypatch):
    other_system = {"letter": "g", "label": "Win", "fs": "FAT32", "total_bytes": 10**10, "free_bytes": 1,
                    "has_media": True, "removable": True, "is_system": True}
    monkeypatch.setattr(wsl_mount, "list_windows_drives", lambda: _listing(system="g", extra=[other_system]))

    resp = await client.post("/api/device/mount", data={"letter": "g"})

    assert wsl.calls == []
    assert "system drive" in resp.text


async def test_an_already_mounted_card_is_reported_without_running_anything(client, wsl):
    wsl.mounts["e"] = "ok"

    resp = await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls == []
    assert "already mounted" in resp.text
    assert "--color-success" in resp.text
    assert resp.headers["HX-Trigger"] == "leasure:drives-changed"


async def test_a_stale_mount_is_unmounted_before_mounting_again(client, wsl):
    wsl.mounts["e"] = "stale"

    await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls[0] == WSL + ["/bin/umount", "/mnt/e"]
    assert wsl.calls[-1] == MOUNT_E


async def test_a_failed_mount_removes_the_mount_point_it_created_and_hides_wsl_noise(client, wsl):
    wsl.replies["/bin/mount"] = (32, "<3>WSL (812) ERROR: UtilTranslatePathList:2866\nmount: wrong fs type")

    resp = await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls[-1] == WSL + ["/bin/rmdir", "/mnt/e"]
    assert "wrong fs type" in resp.text
    assert "&lt;3&gt;WSL" not in resp.text and "<3>WSL" not in resp.text


async def test_a_stale_mount_held_busy_reports_busy_instead_of_mounting(client, wsl):
    wsl.mounts["e"] = "stale"
    wsl.replies["/bin/umount"] = (32, "umount: /mnt/e: target is busy.")

    resp = await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls == [WSL + ["/bin/umount", "/mnt/e"]]
    assert "still has files open on E:" in resp.text


async def test_a_wsl_exe_failure_on_stdout_is_shown_without_utf16_nuls(client, wsl):
    wsl.replies["/bin/mkdir"] = (255, "", "T\x00h\x00e\x00r\x00e\x00 \x00i\x00s\x00 \x00n\x00o\x00 \x00d\x00i\x00s\x00t\x00r\x00o\x00")

    resp = await client.post("/api/device/mount", data={"letter": "e"})

    assert "There is no distro" in resp.text
    assert "\x00" not in resp.text


def test_run_survives_output_that_is_not_utf8():
    result = wsl_mount._run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'M\\x82SICA')"], 10)

    assert result.stdout.startswith("M") and result.stdout.endswith("SICA")


def test_the_drive_listing_asks_powershell_for_utf8():
    assert wsl_mount._LIST_SCRIPT.startswith("[Console]::OutputEncoding=[Text.Encoding]::UTF8;")


def test_overlapping_mounts_of_one_letter_mount_it_once(wsl, monkeypatch):
    in_mount = threading.Event()

    def slow_mount(argv, timeout):
        if "/bin/mount" in argv:
            in_mount.set()
            time.sleep(0.2)
        return wsl(argv, timeout)

    monkeypatch.setattr(wsl_mount, "_run", slow_mount)
    first = threading.Thread(target=wsl_mount.mount, args=("e",))
    first.start()
    assert in_mount.wait(5)

    second = wsl_mount.mount("e")
    first.join()

    assert sum("/bin/mount" in call for call in wsl.calls) == 1
    assert second.status == "already"


async def test_without_interop_sudo_is_used_and_a_password_prompt_shows_manual_commands(client, wsl, monkeypatch):
    monkeypatch.setattr(wsl_mount, "interop_enabled", lambda: False)
    wsl.replies["/bin/mkdir"] = (1, "sudo: a password is required")

    resp = await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls[0][:2] == ["sudo", "-n"]
    assert "--color-error" not in resp.text
    assert "wsl -d Ubuntu -u root" in resp.text
    assert "user-select" in resp.text


async def test_an_unstartable_wsl_exe_falls_back_to_sudo(client, wsl):
    wsl.unstartable = "/fake/wsl.exe"

    await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls[0][0] == "/fake/wsl.exe"
    assert wsl.calls[1] == ["sudo", "-n", "/bin/mkdir", "-p", "/mnt/e"]


async def test_an_unsafe_distro_name_is_never_passed_to_wsl(client, wsl, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu; x")

    await client.post("/api/device/mount", data={"letter": "e"})

    assert wsl.calls and all(call[:2] == ["sudo", "-n"] for call in wsl.calls)


async def test_eject_unmounts_and_says_the_card_is_safe_to_remove(client, wsl):
    wsl.mounts["e"] = "ok"

    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert wsl.calls == [WSL + ["/bin/umount", "/mnt/e"], WSL + ["/bin/rmdir", "/mnt/e"]]
    assert "safe to remove" in resp.text
    assert resp.headers["HX-Trigger"] == "leasure:drives-changed"


async def test_eject_unmounts_stacked_mounts_until_the_letter_is_gone(client, wsl):
    wsl.mounts["e"] = "ok"
    wsl.stacked["e"] = 2

    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert wsl.calls == [WSL + ["/bin/umount", "/mnt/e"]] * 2 + [WSL + ["/bin/rmdir", "/mnt/e"]]
    assert "safe to remove" in resp.text


async def test_eject_never_says_safe_to_remove_while_the_card_is_still_mounted(client, wsl):
    wsl.mounts["e"] = "ok"
    wsl.stacked["e"] = 99

    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert "safe to remove" not in resp.text
    assert "still mounted" in resp.text
    assert WSL + ["/bin/rmdir", "/mnt/e"] not in wsl.calls


async def test_a_sync_cannot_start_on_a_card_that_is_being_ejected(client, wsl, monkeypatch):
    from routers import device as device_router

    seen = []

    def unmount_while_a_sync_starts(letter):
        seen.append("/mnt/e" in device_router._EJECTING)
        return wsl_mount.MountResult("unmounted", "E: unmounted. It is safe to remove the card.", "e")

    monkeypatch.setattr(wsl_mount, "unmount", unmount_while_a_sync_starts)

    await client.post("/api/device/eject", data={"letter": "e"})

    assert seen == [True]
    assert device_router._EJECTING == set()


async def test_eject_of_a_busy_card_explains_what_holds_it(client, wsl):
    wsl.mounts["e"] = "ok"
    wsl.replies["/bin/umount"] = (32, "umount: /mnt/e: target is busy.")

    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert "still has files open on E:" in resp.text
    assert "--color-error" not in resp.text


async def test_eject_is_refused_while_a_sync_writes_to_the_card(client, wsl, monkeypatch):
    from routers import device as device_router

    class Running:
        def done(self):
            return False

    monkeypatch.setitem(device_router._SYNC_JOBS, "job", {"device_path": "/mnt/e", "task": Running(), "created": 0})
    wsl.mounts["e"] = "ok"

    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert wsl.calls == []
    assert "still running" in resp.text


async def test_eject_of_an_unmounted_letter_says_so(client, wsl):
    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert wsl.calls == []
    assert "not mounted" in resp.text


async def test_eject_refuses_the_system_drive(client, wsl):
    wsl.mounts["c"] = "ok"

    resp = await client.post("/api/device/eject", data={"letter": "c"})

    assert wsl.calls == []
    assert "system drive" in resp.text


@pytest.mark.parametrize("route", ["/api/device/mount", "/api/device/eject"])
async def test_outside_wsl2_mount_and_eject_only_explain(client, wsl, monkeypatch, route):
    from routers import device as device_router

    monkeypatch.setattr(device_router, "get_platform", lambda: "linux")

    resp = await client.post(route, data={"letter": "e"})

    assert wsl.calls == []
    assert "--color-error" not in resp.text


async def test_a_cross_site_mount_is_rejected_before_anything_runs(client, wsl):
    resp = await client.post("/api/device/mount", data={"letter": "e"}, headers={"Sec-Fetch-Site": "cross-site"})

    assert resp.status_code == 403
    assert wsl.calls == []


async def test_a_stale_mount_is_reported_and_is_never_a_sync_target(wsl):
    from services.device import detect_stale_wsl2, resolve_sync_target

    wsl.mounts["e"] = "stale"

    assert detect_stale_wsl2() == ["e"]
    assert resolve_sync_target("/mnt/e") is None


async def test_removable_list_offers_the_right_action_per_slot(client, wsl, monkeypatch):
    card = {"letter": "g", "label": "H2", "fs": "FAT32", "total_bytes": 32 * 10**9, "free_bytes": 1,
            "has_media": True, "removable": True, "is_system": False}
    monkeypatch.setattr(wsl_mount, "list_windows_drives", lambda: _listing(extra=[card]))
    wsl.mounts.update({"e": "ok", "f": "stale"})

    resp = await client.get("/api/device/removable/html")

    text = resp.text
    assert "No card" in text
    assert "Mounted at /mnt/e" in text and "Eject" in text
    assert "Card was removed" in text and "Clear" in text
    assert "H2 (G:, FAT32, 29.8 GB)" in text and ">Mount</button>" in text
    assert "(C:" not in text
    assert wsl.calls == []


async def test_removable_list_without_a_windows_listing_offers_a_letter_picker(client, wsl, monkeypatch):
    monkeypatch.setattr(wsl_mount, "list_windows_drives", lambda: None)

    resp = await client.get("/api/device/removable/html")

    assert '<select name="letter"' in resp.text
    assert 'value="c"' not in resp.text
    assert "wsl -d Ubuntu -u root" in resp.text


@pytest.mark.parametrize(("asked", "shown"), [("f", "F"), ("G", "G"), ("c", "E"), ("a", "E"), ("e;x", "E")])
async def test_hand_mount_commands_are_for_the_picked_letter(client, wsl, monkeypatch, asked, shown):
    monkeypatch.setattr(wsl_mount, "list_windows_drives", lambda: None)

    resp = await client.get("/api/device/removable/html", params={"letter": asked})

    assert f"mount -t drvfs {shown}: /mnt/{shown.lower()}" in resp.text


async def test_removable_rows_give_the_size_in_the_unit_the_device_list_uses(wsl):
    from routers import device as device_router

    rows = device_router._removable_drive_rows(_listing(), {})

    assert next(r for r in rows if r["letter"] == "e")["total_gb"] == 116.1


async def test_clearing_a_pulled_card_does_not_say_it_is_safe_to_remove(client, wsl):
    wsl.mounts["e"] = "stale"

    resp = await client.post("/api/device/eject", data={"letter": "e"})

    assert "cleared" in resp.text
    assert "safe to remove" not in resp.text


async def test_the_empty_device_list_leaves_hand_mount_commands_to_the_card_reader(client, wsl):
    resp = await client.get("/api/device/detect/html")

    assert "No card mounted" in resp.text
    assert "Mount by hand" not in resp.text


async def test_an_automount_that_needs_root_shows_commands_for_that_card(client, wsl, monkeypatch):
    card = {"letter": "g", "label": "H2", "fs": "FAT32", "total_bytes": 32 * 10**9, "free_bytes": 1,
            "has_media": True, "removable": True, "is_system": False}
    monkeypatch.setattr(wsl_mount, "list_windows_drives", lambda: _listing(extra=[card]))
    wsl.mounts["e"] = "ok"
    wsl.unstartable = "/fake/wsl.exe"
    wsl.replies["/bin/mkdir"] = (1, "sudo: a password is required")

    resp = await client.post("/api/device/automount")

    assert "Mount by hand" in resp.text
    assert "mount -t drvfs G: /mnt/g" in resp.text


async def test_automount_mounts_inserted_cards_and_clears_pulled_ones(client, wsl):
    wsl.mounts["f"] = "stale"

    resp = await client.post("/api/device/automount")

    assert MOUNT_E in wsl.calls
    assert WSL + ["/bin/umount", "/mnt/f"] in wsl.calls
    assert not any("/mnt/d" in call for call in wsl.calls)
    assert resp.headers["HX-Trigger"] == "leasure:drives-changed"


async def test_a_failing_automount_still_renders_the_device_list(client, wsl, monkeypatch):
    def broken():
        raise UnicodeDecodeError("utf-8", b"\x82", 0, 1, "invalid start byte")

    monkeypatch.setattr(wsl_mount, "automount", broken)

    resp = await client.post("/api/device/automount")

    assert resp.status_code == 200
    assert "No card mounted" in resp.text


async def test_automount_with_nothing_to_do_does_not_announce_a_change(client, wsl):
    wsl.mounts["e"] = "ok"

    resp = await client.post("/api/device/automount")

    assert wsl.calls == []
    assert "HX-Trigger" not in resp.headers


# --- list_windows_drives parsing ---

SAMPLE = {"system": "C:", "disks": [
    {"DeviceID": "C:", "DriveType": 3, "FileSystem": "NTFS", "VolumeName": "OS",
     "Size": 2021324558336, "FreeSpace": 1412521426944},
    {"DeviceID": "D:", "DriveType": 2, "FileSystem": None, "VolumeName": None, "Size": None, "FreeSpace": None},
    {"DeviceID": "E:", "DriveType": 2, "FileSystem": "exFAT", "VolumeName": "",
     "Size": 124656812032, "FreeSpace": 117653766144},
]}


@pytest.fixture
def powershell(monkeypatch):
    reply = {"result": subprocess.CompletedProcess([], 0, json.dumps(SAMPLE), "")}

    def fake_run(argv, timeout):
        result = reply["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(wsl_mount, "_win_exe", lambda name, fallback: "/fake/powershell.exe")
    monkeypatch.setattr(wsl_mount, "_run", fake_run)
    return reply


def test_listing_parses_the_windows_sample(powershell):
    listing = wsl_mount.list_windows_drives()

    assert listing["system"] == "c"
    by_letter = {d["letter"]: d for d in listing["disks"]}
    assert by_letter["d"]["has_media"] is False and by_letter["d"]["removable"] is True
    assert by_letter["e"] == {"letter": "e", "label": "E:", "fs": "exFAT", "total_bytes": 124656812032,
                              "free_bytes": 117653766144, "has_media": True, "removable": True, "is_system": False}
    assert [d["letter"] for d in wsl_mount.removable_drives(listing)] == ["d", "e"]


def test_listing_accepts_a_single_disk_object(powershell):
    powershell["result"] = subprocess.CompletedProcess([], 0, json.dumps({"system": "C:", "disks": SAMPLE["disks"][2]}), "")

    assert [d["letter"] for d in wsl_mount.list_windows_drives()["disks"]] == ["e"]


def test_a_fixed_exfat_disk_counts_as_removable(powershell):
    disk = {"DeviceID": "F:", "DriveType": 3, "FileSystem": "exFAT", "VolumeName": "H2", "Size": 10, "FreeSpace": 1}
    powershell["result"] = subprocess.CompletedProcess([], 0, json.dumps({"system": "C:", "disks": [disk]}), "")

    assert wsl_mount.removable_drives(wsl_mount.list_windows_drives())[0]["label"] == "H2"


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, "", "boom"),
    subprocess.CompletedProcess([], 0, "not json", ""),
    subprocess.TimeoutExpired("powershell.exe", 8),
    OSError(errno.ENOEXEC, "Exec format error"),
])
def test_listing_failures_return_none(powershell, result):
    powershell["result"] = result

    assert wsl_mount.list_windows_drives() is None
