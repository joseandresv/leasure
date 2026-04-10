import logging
import os
import re
import shutil
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

FAT32_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str, max_length: int = 200) -> str:
    name = FAT32_FORBIDDEN.sub("_", name)
    name = name.strip(". ")
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    return name or "Unknown"


def _get_mounted_drives() -> dict[str, str]:
    """Read /proc/mounts to find actual Windows drive mounts (drvfs/9p)."""
    mounted = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1].startswith("/mnt/") and len(parts[1]) == 6:
                    # e.g. "C:\ /mnt/c 9p ..." or "E:\ /mnt/e drvfs ..."
                    mount_point = parts[1]
                    fs_type = parts[2]
                    if fs_type in ("9p", "drvfs", "vfat", "exfat", "fuseblk"):
                        letter = mount_point[-1]
                        mounted[letter] = fs_type
    except OSError:
        pass
    return mounted


def detect_devices() -> list[dict]:
    """Scan /mnt/ drive letters for mounted volumes. Categorizes as system vs removable."""
    mounted_drives = _get_mounted_drives()
    candidates = []
    for letter in "cdefghijklmnopqrstuvwxyz":
        path = f"/mnt/{letter}"
        if letter not in mounted_drives:
            continue
        if not os.path.isdir(path):
            continue
        try:
            contents = os.listdir(path)

            usage = shutil.disk_usage(path)
            if usage.total == 0:
                continue

            total_gb = usage.total / (1024**3)
            free_gb = usage.free / (1024**3)
            used_gb = usage.used / (1024**3)

            # Detect if it looks like a music player / SD card
            has_music_files = any(
                f.endswith((".mp3", ".flac", ".wav", ".ape", ".dsf"))
                for f in contents
            ) or any(
                os.path.isdir(os.path.join(path, d)) and d not in ("$RECYCLE.BIN", "System Volume Information", "Windows", "Program Files", "Users")
                for d in contents
            )
            is_fat32 = _check_fat32(path)

            # Guess device type
            if total_gb > 500:
                device_type = "system"
            elif total_gb <= 512 and (has_music_files or is_fat32):
                device_type = "player"
            elif total_gb <= 512:
                device_type = "removable"
            else:
                device_type = "drive"

            candidates.append({
                "path": path,
                "drive_letter": letter.upper(),
                "total_gb": round(total_gb, 1),
                "free_gb": round(free_gb, 1),
                "used_gb": round(used_gb, 1),
                "has_music_files": has_music_files,
                "device_type": device_type,
                "file_count": len(contents),
            })
        except OSError:
            continue

    # Sort: player/removable drives first, then by letter
    type_order = {"player": 0, "removable": 1, "drive": 2, "system": 3}
    candidates.sort(key=lambda d: (type_order.get(d["device_type"], 9), d["drive_letter"]))
    return candidates


def _check_fat32(path: str) -> bool:
    """Heuristic: FAT32/exFAT drives on WSL2 are mounted via drvfs."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == path:
                    return parts[2] in ("vfat", "exfat", "fuseblk", "drvfs", "9p")
    except OSError:
        pass
    return False


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
