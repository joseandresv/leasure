import hashlib
import logging
import os
import re
import shutil
import string
from pathlib import Path

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

# Entries at the card root that belong to the player firmware or the OS: never listed, imported,
# written, swept or deleted, nor anything beneath them. Every card deletion must check
# is_protected_card_path / is_protected_on_card first. Compared case-insensitively (FAT is).
# The stock HiBy firmware keeps no folder of its own on the card that the code knows of.
PROTECTED_ROOT_NAMES = frozenset(n.casefold() for n in (
    ".rockbox",
    "System Volume Information", "$RECYCLE.BIN",
    ".Trashes", ".Spotlight-V100", ".fseventsd",
))
_TRASH_DIR = re.compile(r"\.trash-\d+", re.IGNORECASE)

CARD_PROFILE_ROCKBOX, CARD_PROFILE_STOCK = "rockbox", "stock"
ROCKBOX_DB_REMINDER = ("On the player: Settings → General Settings → Database → Initialize Now "
                       "(or turn on Auto Update) so new songs appear in the Database.")

# Rockbox's MAX_PATH is 260 bytes including the NUL; card paths are "/" + rel in UTF-8.
MAX_CARD_PATH_BYTES = 250
# Folder caps depend on the folders alone, so every track of an album lands in one folder.
MAX_ARTIST_DIR_BYTES = 200
MAX_DIRS_BYTES = 210
# A trimmed stem is the same whatever the audio extension (".flac"/".opus" are the longest).
FILE_EXT_RESERVE = 5
# A trimmed file name plus ".leasure-part" stays within FAT's 255 units.
MAX_TRIMMED_STEM_BYTES = 235
_TRIM_HASH_HEX = 6

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


def is_protected_card_path(rel: str | os.PathLike) -> bool:
    """True when `rel`, relative to the card root, names a protected root entry or anything beneath it.
    A path that climbs ('..') or is empty counts as protected: it cannot be shown to stay outside them."""
    parts = [p for p in os.fspath(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return True
    first = parts[0].casefold()
    return first in PROTECTED_ROOT_NAMES or bool(_TRASH_DIR.fullmatch(first))


def is_protected_on_card(root: str | os.PathLike, path: str | os.PathLike) -> bool:
    """`path` on the card at `root`: protected if its relative path is, if it is not under `root`
    (the root itself included), or if it resolves through a link into a protected entry."""
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        return True
    if is_protected_card_path(rel.as_posix()):
        return True
    if not os.path.lexists(path):
        return False
    try:
        real = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:
        return True
    return is_protected_card_path(real)


def is_link(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def safe_card_rel(rel: str) -> bool:
    """Artist/Album/File as build_device_path makes it: relative, POSIX, nothing that climbs out."""
    parts = rel.split("/")
    return len(parts) == 3 and "\\" not in rel and ":" not in rel and all(p and p not in (".", "..") for p in parts)


def inside_real_dirs(target: Path, rel: str) -> bool:
    """A safe_card_rel path whose artist and album folders are real folders inside the card."""
    artist, album, _ = rel.split("/")
    if is_link(target / artist) or is_link(target / artist / album):
        return False
    return target.resolve() in (target / rel).resolve().parents


def remove_empty_card_dirs(target: Path, folder: Path) -> None:
    """rmdir `folder` and then its parent while they are empty; never the root or a protected folder."""
    root = target.resolve()
    for d in (folder, folder.parent):
        if root not in d.resolve().parents or is_protected_on_card(target, d):
            return
        try:
            d.rmdir()
        except OSError:
            return


def card_profile(root: str | os.PathLike) -> str:
    """"rockbox" when the card root holds a .rockbox folder (any case), else "stock". Read-only;
    an unreadable root counts as stock."""
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.casefold() == ".rockbox" and entry.is_dir(follow_symlinks=False):
                    return CARD_PROFILE_ROCKBOX
    except OSError:
        pass
    return CARD_PROFILE_STOCK


_FS_ALIASES = {"fat": "fat", "fat12": "fat", "fat16": "fat", "fat32": "fat", "vfat": "fat", "msdos": "fat",
               "exfat": "exfat", "ntfs": "ntfs", "ntfs3": "ntfs"}
# Bridges and FUSE mounts that do not tell what the card itself is formatted with.
_OPAQUE_FS = {"fuseblk", "drvfs", "9p", "v9fs"}
_FS_LABELS = {"fat": "FAT32", "exfat": "exFAT", "ntfs": "NTFS"}


def _normalise_fs(name: str | None) -> str | None:
    key = (name or "").strip().lower()
    if not key or key in _OPAQUE_FS:
        return None
    return _FS_ALIASES.get(key, key)


def _windows_fs(path: str) -> str | None:
    import ctypes

    fs_buf = ctypes.create_unicode_buffer(64)
    anchor = Path(path).anchor or path
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(anchor), None, 0, None, None, None, fs_buf, 64)
    return fs_buf.value if ok else None


def _wsl2_fs(real: str) -> str | None:
    from services import wsl_mount

    letter = real.removeprefix("/mnt/").split("/")[0]
    listing = wsl_mount.list_windows_drives()
    disk = next((d for d in (listing or {}).get("disks", []) if d.get("letter") == letter), None)
    return disk.get("fs") if disk else None


def _mounted_fs(real: str) -> str | None:
    best, fs = "", None
    for mount_point, fs_type in _read_proc_mounts():
        prefix = mount_point.rstrip("/") + "/"
        if (real == mount_point or real.startswith(prefix)) and len(mount_point) > len(best):
            best, fs = mount_point, fs_type
    return fs


def card_filesystem(path: str) -> str | None:
    """"fat" | "exfat" | "ntfs" | another lowercase name, or None when it cannot be known.
    On WSL2 this asks Windows through PowerShell, so it is slow: call it in a thread, and only when needed."""
    plat = get_platform()
    if plat == "windows":
        return _normalise_fs(_windows_fs(path))
    real = os.path.realpath(path)
    if plat == "wsl2" and re.fullmatch(r"/mnt/[a-z](/.*)?", real):
        return _normalise_fs(_wsl2_fs(real))
    return _normalise_fs(_mounted_fs(real))


def rockbox_fs_warning(fs: str | None) -> str | None:
    """The sync-preview warning for a Rockbox card whose filesystem is known and is not FAT."""
    if fs is None or fs == "fat":
        return None
    return (f"This card is formatted {_FS_LABELS.get(fs, fs.upper())}. Rockbox reads FAT32 cards only; "
            "use a FAT32 card for the Rockbox trial.")


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
        os.path.isdir(os.path.join(path, d)) and d not in SYSTEM_DIR_NAMES and not is_protected_card_path(d)
        for d in contents
    )
    is_rockbox = any(n.casefold() == ".rockbox" and os.path.isdir(os.path.join(path, n)) for n in contents)

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
        "profile": CARD_PROFILE_ROCKBOX if is_rockbox else CARD_PROFILE_STOCK,
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
    from services import wsl_mount

    candidates = []
    for letter, state in wsl_mount.mounted_letters().items():
        if state != "ok":
            continue
        device = _classify(f"/mnt/{letter}", f"{letter.upper()}:", letter.upper(), removable_hint=True)
        if device:
            candidates.append(device)
    return candidates


def detect_stale_wsl2() -> list[str]:
    """Letters still in the mount table whose card was pulled; never sync targets."""
    from services import wsl_mount

    return [letter for letter, state in wsl_mount.mounted_letters().items() if state == "stale"]


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


def artist_folder(artist: str | None, album_artist: str | None = None) -> str:
    """Folder name for a track: the album credit when the provider gave a real one, else the
    first artist before a comma. The comma split is a last resort — it mangles names
    like "Earth, Wind & Fire" — but it keeps a featured-artist track in the album folder.
    The full artist list is preserved in the tags (TPE2 always carries the whole album
    artist) for the H2's Category browser.

    Rows written before Sprint 2 stored the full track artist as album_artist, so a
    comma-joined album credit identical to the artist is not an album credit at all: it is
    split like a bare artist, which keeps those cards' existing folders stable instead of
    re-copying every featured-artist track under a new name."""
    credit = (album_artist or "").strip()
    track_artist = (artist or "").strip()
    if not credit or (credit == track_artist and "," in credit):
        credit = (track_artist or "Unknown Artist").split(",")[0].strip()
    folder = sanitize_filename(credit or "Unknown Artist")
    # An artist literally named like a system folder must not write into it.
    return f"_{folder}" if is_protected_card_path(folder) else folder


def track_prefix(track_number: int | None, disc_number: int | None = None, total_discs: int | None = None) -> str:
    """"NN - " for a single disc, "D-NN - " on a multi-disc album, "" without a number."""
    if not track_number:
        return ""
    if (total_discs or 0) > 1 or (disc_number or 1) > 1:
        return f"{disc_number or 1}-{track_number:02d} - "
    return f"{track_number:02d} - "


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _trim_utf8(text: str, max_bytes: int) -> str:
    """`text` cut to `max_bytes` UTF-8 bytes on a character boundary, tagged with a hash of the full
    name so two long names sharing their first bytes stay apart. Unchanged when it already fits."""
    if _utf8_len(text) <= max_bytes:
        return text
    tag = "~" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:_TRIM_HASH_HEX]
    cut = text.encode("utf-8")[:max_bytes - len(tag)].decode("utf-8", "ignore").rstrip(". ")
    return cut + tag


def build_device_path(artist: str, album: str, track_number: int | None, title: str, ext: str,
                      album_artist: str | None = None, disc_number: int | None = None,
                      total_discs: int | None = None) -> str:
    """Artist/Album/NN - Title.ext on the card, within Rockbox's byte limits. Folders past their caps are
    shortened whatever the title, so an album stays in one folder; then only an over-limit title is.
    Every path of up to 209 bytes is therefore the same as before the limits existed."""
    artist_dir = _trim_utf8(artist_folder(artist, album_artist), MAX_ARTIST_DIR_BYTES)
    album_dir = _trim_utf8(sanitize_filename(album or "Unknown Album"), MAX_DIRS_BYTES - _utf8_len(artist_dir))
    prefix = track_prefix(track_number, disc_number, total_discs)
    filename = sanitize_filename(f"{prefix}{title}")
    # No MUSIC/ prefix — put artist folders directly on the SD card root
    # The H2 scans the entire card and reads ID3 tags for Category browsing
    rel = f"{artist_dir}/{album_dir}/{filename}.{ext}"
    if _utf8_len("/" + rel) <= MAX_CARD_PATH_BYTES:
        return rel
    kept = prefix if prefix and filename.startswith(prefix) else ""
    ext_reserve = max(FILE_EXT_RESERVE, 1 + _utf8_len(ext))
    budget = min(MAX_TRIMMED_STEM_BYTES,
                 MAX_CARD_PATH_BYTES - 3 - _utf8_len(artist_dir) - _utf8_len(album_dir) - ext_reserve)
    filename = kept + _trim_utf8(filename[len(kept):], budget - _utf8_len(kept))
    return f"{artist_dir}/{album_dir}/{filename}.{ext}"


def sidecar_rel(audio_rel: str, suffix: str) -> str:
    """The card path of a sidecar (".jpg"/".lrc") next to `audio_rel`: the same stem."""
    return audio_rel.rsplit(".", 1)[0] + suffix
