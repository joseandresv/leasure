import logging
import os
import re
import shutil
import string

from services.platform import get_platform

logger = logging.getLogger(__name__)

# FAT32-forbidden characters plus ASCII control characters
FAT32_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

AUDIO_EXTS = (".mp3", ".flac", ".wav", ".ape", ".dsf", ".dff", ".m4a", ".opus", ".ogg")

# Folders that mark a drive as a Windows system volume, not a music player
SYSTEM_DIR_NAMES = ("$RECYCLE.BIN", "System Volume Information", "Windows", "Program Files", "Users")
# A Windows root, or a Program Files + Users pair, means an OS volume whatever its size.
# "Users" alone is not enough: cards people organise by user name would be locked out.
OS_MARKER_DIRS = ("Windows",)
_PROGRAM_FILES_DIRS = ("Program Files", "Program Files (x86)")

# Windows reserves these basenames (case-insensitive, with or without extension)
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def sanitize_filename(name: str, max_length: int = 200) -> str:
    name = FAT32_FORBIDDEN.sub("_", name or "")
    name = name.strip(". ")
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"
    return name or "Unknown"


def is_system_volume(contents: list[str]) -> bool:
    if any(d in contents for d in OS_MARKER_DIRS):
        return True
    return "Users" in contents and any(d in contents for d in _PROGRAM_FILES_DIRS)


def _classify(path: str, label: str, drive_letter: str, removable_hint: bool) -> dict | None:
    """Shared heuristics: disk usage, music detection, device-type guess."""
    try:
        contents = os.listdir(path)
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.total == 0:
        return None

    total_gb = usage.total / (1024**3)
    free_gb = usage.free / (1024**3)
    used_gb = usage.used / (1024**3)

    has_music_files = any(
        f.endswith(AUDIO_EXTS) for f in contents
    ) or any(
        os.path.isdir(os.path.join(path, d)) and d not in SYSTEM_DIR_NAMES
        for d in contents
    )

    # Guess device type: FAT32/exFAT and removable media are player candidates.
    # An OS volume is never a sync target, however small it is.
    if total_gb > 500 or is_system_volume(contents):
        device_type = "system"
    elif total_gb <= 512 and (has_music_files or removable_hint):
        device_type = "player"
    elif total_gb <= 512:
        device_type = "removable"
    else:
        device_type = "drive"

    return {
        "path": path,
        "drive_letter": drive_letter,
        "label": label,
        "total_gb": round(total_gb, 1),
        "free_gb": round(free_gb, 1),
        "used_gb": round(used_gb, 1),
        "has_music_files": has_music_files,
        "device_type": device_type,
        "file_count": len(contents),
    }


def _read_proc_mounts() -> list[tuple[str, str]]:
    """Return (mount_point, fs_type) pairs from /proc/mounts, unescaping octal codes."""
    mounts = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    # /proc/mounts escapes spaces as \040, tabs as \011, backslashes as \134
                    mount_point = (
                        parts[1].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
                    )
                    mounts.append((mount_point, parts[2]))
    except OSError:
        pass
    return mounts


REMOVABLE_FS_TYPES = ("vfat", "exfat", "fuseblk", "drvfs", "9p", "ntfs", "ntfs3")


def _detect_wsl2() -> list[dict]:
    """Scan /mnt/<letter> drvfs/9p mounts (Windows drives bridged into WSL2)."""
    mounted = {}
    for mount_point, fs_type in _read_proc_mounts():
        if mount_point.startswith("/mnt/") and len(mount_point) == 6 and fs_type in REMOVABLE_FS_TYPES:
            mounted[mount_point[-1]] = fs_type

    candidates = []
    for letter in mounted:
        path = f"/mnt/{letter}"
        if not os.path.isdir(path):
            continue
        device = _classify(path, f"{letter.upper()}:", letter.upper(), removable_hint=True)
        if device:
            candidates.append(device)
    return candidates


def _detect_linux() -> list[dict]:
    """Scan desktop auto-mount locations (/media/<user>/, /run/media/<user>/)."""
    candidates = []
    for mount_point, fs_type in _read_proc_mounts():
        if not (mount_point.startswith("/media/") or mount_point.startswith("/run/media/")):
            continue
        if not os.path.isdir(mount_point):
            continue
        label = os.path.basename(mount_point) or mount_point
        device = _classify(mount_point, label, "", removable_hint=fs_type in REMOVABLE_FS_TYPES)
        if device:
            candidates.append(device)
    return candidates


def _detect_windows() -> list[dict]:
    """Enumerate drive letters via the Win32 API."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    # Don't pop "insert a disk" dialogs for empty card-reader slots
    kernel32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS

    DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3
    bitmask = kernel32.GetLogicalDrives()
    candidates = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not bitmask & (1 << i):
            continue
        root = f"{letter}:\\"
        drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if drive_type not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue

        fs_buf = ctypes.create_unicode_buffer(64)
        vol_buf = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), vol_buf, 261, None, None, None, fs_buf, 64
        )
        is_fat = bool(ok) and fs_buf.value.upper() in ("FAT", "FAT32", "EXFAT")
        label = vol_buf.value if (ok and vol_buf.value) else f"{letter}:"
        device = _classify(
            root, label, letter, removable_hint=(drive_type == DRIVE_REMOVABLE or is_fat)
        )
        if device:
            candidates.append(device)
    return candidates


def detect_devices() -> list[dict]:
    """Scan for mounted volumes on the current platform, players first."""
    plat = get_platform()
    if plat == "windows":
        candidates = _detect_windows()
    elif plat == "wsl2":
        candidates = _detect_wsl2()
    else:
        candidates = _detect_linux()

    type_order = {"player": 0, "removable": 1, "drive": 2, "system": 3}
    candidates.sort(key=lambda d: (type_order.get(d["device_type"], 9), d["label"]))
    return candidates


def sync_targets() -> list[dict]:
    """Volumes the app is allowed to write to: every detected volume except OS/system ones."""
    return [d for d in detect_devices() if d["device_type"] != "system"]


def resolve_sync_target(device_path: str) -> str | None:
    """Return the canonical path of `device_path` if it is an allowed sync target, else None.

    The sync endpoints write and delete files under the target, so the target must be
    one of the volumes detect_devices() found (never the system drive) — a free-form
    path from the request is not trusted."""
    if not device_path:
        return None
    try:
        wanted = os.path.normcase(os.path.realpath(device_path))
    except OSError:
        return None
    for d in sync_targets():
        try:
            if os.path.normcase(os.path.realpath(d["path"])) == wanted:
                return d["path"]
        except OSError:
            continue
    return None


def build_device_path(artist: str, album: str, track_number: int | None, title: str, ext: str) -> str:
    # Use primary artist only for folder (keeps album tracks together)
    # Full artist list is preserved in ID3 tags for the H2's Category browser
    primary_artist = (artist or "Unknown Artist").split(",")[0].strip()
    artist_dir = sanitize_filename(primary_artist)
    album_dir = sanitize_filename(album or "Unknown Album")
    num_prefix = f"{track_number:02d} - " if track_number else ""
    filename = sanitize_filename(f"{num_prefix}{title}")
    # No MUSIC/ prefix — put artist folders directly on the SD card root
    # The H2 scans the entire card and reads ID3 tags for Category browsing
    return f"{artist_dir}/{album_dir}/{filename}.{ext}"
