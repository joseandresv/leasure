"""Mount and eject Windows drive letters inside WSL2 without a sudo password.

Root comes from `wsl.exe -d <distro> -u root -e ...` through Windows interop (no password
prompt), with `sudo -n` as the fallback. Every command is an absolute binary from a fixed
set, never a shell string. Only used when get_platform() == "wsl2"."""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass

from services import device as device_service

logger = logging.getLogger(__name__)

LETTER_RE = re.compile(r"^[a-z]$")
DISTRO_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HX_DRIVES_CHANGED = "leasure:drives-changed"

_WSL_FALLBACK = "/mnt/c/Windows/System32/wsl.exe"
_POWERSHELL_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_ROOT_BINARIES = frozenset({"/bin/mkdir", "/bin/mount", "/bin/umount", "/bin/rmdir"})
_FAT_FILESYSTEMS = ("FAT", "FAT32", "EXFAT")
# Without the OutputEncoding line PowerShell 5.1 writes redirected output in the OEM code
# page (437/850), so a label like "Música" would not be UTF-8.
_LIST_SCRIPT = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "$d=@(Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -in 2,3 } | "
    "Select-Object DeviceID,DriveType,FileSystem,VolumeName,Size,FreeSpace); "
    "ConvertTo-Json -Compress -Depth 3 -InputObject @{system=$env:SystemDrive; disks=$d}"
)
# drvfs mounts stack (each gets its own superblock), so one umount may leave another below.
_MAX_STACKED_UMOUNTS = 4
# One mount/unmount at a time: the page load, Scan and a second tab can all ask at once.
_MOUNT_LOCK = threading.Lock()


@dataclass
class MountResult:
    status: str
    message: str
    letter: str = ""
    manual: dict | None = None


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", cwd="/", timeout=timeout)


def _win_exe(name: str, fallback: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return fallback if os.path.exists(fallback) else None


def interop_enabled() -> bool:
    for path in glob.glob("/proc/sys/fs/binfmt_misc/WSLInterop*"):
        try:
            with open(path) as handle:
                if handle.readline().strip() == "enabled":
                    return True
        except OSError:
            continue
    return False


def distro() -> str | None:
    name = os.environ.get("WSL_DISTRO_NAME", "")
    return name if DISTRO_RE.fullmatch(name) else None


def _root_prefixes() -> list[list[str]]:
    prefixes = []
    wsl = _win_exe("wsl.exe", _WSL_FALLBACK) if interop_enabled() else None
    name = distro()
    if wsl and name:
        prefixes.append([wsl, "-d", name, "-u", "root", "-e"])
    if shutil.which("sudo"):
        prefixes.append(["sudo", "-n"])
    return prefixes


def _strip_wsl_noise(text: str | None) -> str:
    return "\n".join(line for line in (text or "").replace("\x00", "").splitlines() if not line.startswith("<3>WSL"))


def _without_wsl_noise(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    # wsl.exe prefixes its own diagnostics with "<3>WSL", and writes its own failures to
    # stdout as UTF-16 (NUL between every character).
    return subprocess.CompletedProcess(result.args, result.returncode, _strip_wsl_noise(result.stdout),
                                       _strip_wsl_noise(result.stderr))


def run_as_root(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run one allow-listed binary as root; raises OSError when no root helper can start."""
    if not args or args[0] not in _ROOT_BINARIES:
        raise ValueError(f"refusing to run {args[:1]} as root")
    error: OSError = OSError("neither wsl.exe interop nor sudo is available")
    for prefix in _root_prefixes():
        try:
            return _without_wsl_noise(_run(prefix + args, timeout))
        except OSError as e:
            logger.info("Root helper %s unavailable: %s", prefix[0], e)
            error = e
    raise error


def _disk(raw: dict, system: str) -> dict | None:
    letter = str(raw.get("DeviceID") or "")[:1].lower()
    if not LETTER_RE.fullmatch(letter):
        return None
    fs = raw.get("FileSystem") or ""
    size = raw.get("Size")
    drive_type = raw.get("DriveType")
    return {
        "letter": letter,
        "label": raw.get("VolumeName") or f"{letter.upper()}:",
        "fs": fs,
        "total_bytes": size,
        "free_bytes": raw.get("FreeSpace"),
        "has_media": isinstance(size, int) and size > 0,
        "removable": drive_type == 2 or (drive_type == 3 and fs.upper() in _FAT_FILESYSTEMS),
        "is_system": letter in (system, "c"),
    }


def list_windows_drives() -> dict | None:
    """Removable and fixed Windows volumes via PowerShell, or None when Windows cannot be asked."""
    powershell = _win_exe("powershell.exe", _POWERSHELL_FALLBACK)
    if not powershell:
        return None
    try:
        result = _run([powershell, "-NoProfile", "-NonInteractive", "-Command", _LIST_SCRIPT], 8)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.info("Could not list Windows drives: %s", e)
        return None
    if result.returncode != 0:
        logger.info("Listing Windows drives failed (rc %s): %s", result.returncode, (result.stderr or "")[:200])
        return None
    try:
        data = json.loads((result.stdout or "").lstrip("﻿"))
    except ValueError:
        logger.info("Unreadable Windows drive listing: %s", (result.stdout or "")[:200])
        return None
    if not isinstance(data, dict):
        return None
    system = str(data.get("system") or "C:")[:1].lower()
    disks = data.get("disks") or []
    if isinstance(disks, dict):
        disks = [disks]
    normalised = [d for d in (_disk(raw, system) for raw in disks if isinstance(raw, dict)) if d]
    return {"system": system, "disks": normalised}


def removable_drives(listing: dict) -> list[dict]:
    return [d for d in listing["disks"] if d["removable"] and not d["is_system"]]


def _listable(path: str) -> bool:
    try:
        os.stat(path)
        os.listdir(path)
    except OSError:
        return False
    return True


def mounted_letters() -> dict[str, str]:
    """Letter -> "ok" | "stale" for every /mnt/<letter> drvfs mount (stale: the card is gone)."""
    states = {}
    for mount_point, fs_type in device_service._read_proc_mounts():
        letter = mount_point.removeprefix("/mnt/")
        if mount_point.startswith("/mnt/") and LETTER_RE.fullmatch(letter) \
                and fs_type in device_service.REMOVABLE_FS_TYPES:
            states[letter] = "ok" if _listable(mount_point) else "stale"
    return states


def manual_commands(letter: str) -> dict:
    """Commands the user can paste when Leasure cannot get root itself."""
    lower, upper = letter.lower(), letter.upper()
    command = (f"mkdir -p /mnt/{lower} && mount -t drvfs {upper}: /mnt/{lower} "
               f"-o uid={os.getuid()},gid={os.getgid()}")
    name = distro()
    target = f"-d {name} " if name else ""
    return {"powershell": f'wsl {target}-u root -e sh -c "{command}"', "terminal": f'sudo sh -c "{command}"'}


def _valid_letter(raw: str) -> tuple[str, MountResult | None]:
    letter = (raw or "").strip().lower()
    if not LETTER_RE.fullmatch(letter):
        return "", MountResult("refused", f"'{(raw or '').strip()[:10]}' is not a drive letter.")
    if letter == "c":
        return letter, _system_refusal(letter)
    return letter, None


def _system_refusal(letter: str) -> MountResult:
    return MountResult("refused", f"{letter.upper()}: is the Windows system drive. Leasure never mounts or ejects it.",
                       letter)


def _failure(letter: str, stderr: str) -> MountResult:
    lowered = stderr.lower()
    upper = letter.upper()
    if "no medium" in lowered:
        return MountResult("no_media", f"No card in {upper}:", letter)
    if "does not exist" in lowered:
        return MountResult("no_drive", f"Windows has no drive {upper}:", letter)
    if "password" in lowered or "authentication" in lowered:
        return MountResult("needs_manual", f"Leasure could not get root to mount {upper}:. Run one of these yourself:",
                           letter, manual_commands(letter))
    return MountResult("error", f"Mounting {upper}: failed: {stderr.strip()[:200] or 'unknown error'}", letter)


def _root_unavailable(letter: str) -> MountResult:
    return MountResult("needs_manual", f"Leasure could not get root to mount {letter.upper()}:. Run one of these yourself:",
                       letter, manual_commands(letter))


def _remove_mount_point(mount_point: str) -> None:
    try:
        run_as_root(["/bin/rmdir", mount_point], 5)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.info("Could not remove %s: %s", mount_point, e)


def _umount_all(letter: str) -> MountResult | None:
    """umount /mnt/<letter> until it leaves the mount table; None once it has, else why not."""
    upper, mount_point = letter.upper(), f"/mnt/{letter}"
    for _ in range(_MAX_STACKED_UMOUNTS):
        result = run_as_root(["/bin/umount", mount_point], 15)
        stderr = result.stderr.lower()
        if result.returncode != 0 and "busy" in stderr:
            return MountResult("busy", f"Something still has files open on {upper}: (a terminal in {mount_point}, "
                                       "a file browser). Close it and try again.", letter)
        if result.returncode != 0 and "not mounted" not in stderr:
            return MountResult("error", f"Unmounting {upper}: failed: "
                                        f"{(result.stderr or result.stdout).strip()[:200] or 'unknown error'}", letter)
        if letter not in mounted_letters():
            return None
    return MountResult("error", f"{upper}: is still mounted at {mount_point}. Press Eject again.", letter)


def _mount(letter: str) -> MountResult:
    upper, mount_point = letter.upper(), f"/mnt/{letter}"
    state = mounted_letters().get(letter)
    if state == "ok":
        return MountResult("already", f"{upper}: is already mounted at {mount_point}.", letter)
    try:
        if state == "stale" and (failure := _umount_all(letter)):
            return failure
        existed = os.path.lexists(mount_point)
        result = run_as_root(["/bin/mkdir", "-p", mount_point], 5)
        if result.returncode == 0:
            result = run_as_root(["/bin/mount", "-t", "drvfs", f"{upper}:", mount_point,
                                  "-o", f"uid={os.getuid()},gid={os.getgid()}"], 10)
    except OSError:
        return _root_unavailable(letter)
    except subprocess.TimeoutExpired:
        return MountResult("error", f"Mounting {upper}: timed out. Check the card is inserted, then press Scan.", letter)
    if result.returncode == 0:
        if mounted_letters().get(letter) == "ok":
            return MountResult("mounted", f"{upper}: mounted at {mount_point}.", letter)
        return MountResult("error", f"{upper}: mounted, but {mount_point} is not readable here "
                                    "(mounted in another distro?).", letter)
    if not existed:
        _remove_mount_point(mount_point)
    return _failure(letter, result.stderr or result.stdout or "")


def mount(letter: str) -> MountResult:
    """Mount a removable Windows drive letter at /mnt/<letter> (drvfs, owned by this user)."""
    letter, refusal = _valid_letter(letter)
    if refusal:
        return refusal
    listing = list_windows_drives()
    if listing is not None:
        if letter == listing["system"]:
            return _system_refusal(letter)
        disk = next((d for d in removable_drives(listing) if d["letter"] == letter), None)
        if disk is None:
            return MountResult("no_drive", f"Windows has no removable drive {letter.upper()}:", letter)
        if not disk["has_media"]:
            return MountResult("no_media", f"No card in {letter.upper()}:", letter)
    with _MOUNT_LOCK:
        return _mount(letter)


def _unmount(letter: str) -> MountResult:
    upper, mount_point = letter.upper(), f"/mnt/{letter}"
    state = mounted_letters().get(letter)
    if state is None:
        return MountResult("not_mounted", f"{upper}: is not mounted.", letter)
    try:
        failure = _umount_all(letter)
    except OSError:
        return MountResult("needs_manual", f"Leasure could not get root to unmount {upper}:. "
                                           f"Run: wsl -u root -e umount {mount_point}", letter)
    except subprocess.TimeoutExpired:
        return MountResult("error", f"Unmounting {upper}: timed out. Try again.", letter)
    if failure:
        return failure
    _remove_mount_point(mount_point)
    if state == "stale":
        return MountResult("unmounted", f"{upper}: cleared. The card had already been removed.", letter)
    return MountResult("unmounted", f"{upper}: unmounted. It is safe to remove the card.", letter)


def unmount(letter: str) -> MountResult:
    letter, refusal = _valid_letter(letter)
    if refusal:
        return refusal
    listing = list_windows_drives()
    if listing is not None and letter == listing["system"]:
        return _system_refusal(letter)
    with _MOUNT_LOCK:
        return _unmount(letter)


def automount() -> list[MountResult]:
    """Mount every removable drive that has a card, clear mounts whose card was pulled."""
    listing = list_windows_drives()
    if listing is None:
        return []
    with _MOUNT_LOCK:
        states = mounted_letters()
        with_media = set()
        results = []
        for disk in removable_drives(listing):
            letter = disk["letter"]
            if disk["has_media"]:
                with_media.add(letter)
                if states.get(letter) != "ok":
                    results.append(_mount(letter))
        for letter, state in states.items():
            if state == "stale" and letter not in with_media and letter not in (listing["system"], "c"):
                results.append(_unmount(letter))
        return results
