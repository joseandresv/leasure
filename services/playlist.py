"""
M3U playlist generation for HIFI WALKER H2.

H2 playlist rules:
- Playlist files live at the ROOT of the SD card
- Paths inside are RELATIVE to the SD card root (no leading slash)
- Use forward slashes for path separators
- .m3u8 extension + UTF-8 BOM so non-ASCII filenames resolve
- CRLF line endings for FAT32 / embedded-player compatibility
"""
import logging
from pathlib import Path

from services.device import build_device_path

logger = logging.getLogger(__name__)

PLAYLIST_EXT = ".m3u8"


def generate_m3u(playlist_name: str, tracks: list[dict], device_root: Path) -> Path:
    """Generate a playlist file at the root of the SD card."""
    safe_name = "".join(c for c in playlist_name if c not in r'\/:*?"<>|').strip()
    if not safe_name:
        safe_name = "playlist"

    m3u_path = device_root / f"{safe_name}{PLAYLIST_EXT}"

    lines = []
    for track in tracks:
        ext = track.get("format", "mp3")
        rel_path = build_device_path(
            track["artist"],
            track.get("album", ""),
            track.get("track_number"),
            track["title"],
            ext,
        )
        lines.append(rel_path)

    body = "\r\n".join(lines) + "\r\n"
    m3u_path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    logger.info("Generated playlist '%s' with %d tracks at %s", playlist_name, len(tracks), m3u_path)
    return m3u_path


def generate_all_playlists(playlists: dict[str, list[dict]], device_root: Path) -> list[Path]:
    """Generate multiple playlists at the root of the SD card."""
    generated = []
    for name, tracks in playlists.items():
        if tracks:
            path = generate_m3u(name, tracks, device_root)
            generated.append(path)
    return generated


def sweep_orphan_playlists(device_root: Path, keep_names: set[str]) -> list[str]:
    """
    Delete .m3u / .m3u8 files at device root whose stem isn't in keep_names.

    `keep_names` must contain the sanitized stems that generate_m3u would produce
    (same sanitization: forbidden FAT32 chars stripped, whitespace trimmed).
    """
    removed = []
    for f in device_root.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".m3u", ".m3u8"):
            continue
        if f.stem in keep_names:
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as e:
            logger.warning("Failed to remove orphan playlist %s: %s", f, e)
    return removed


def sanitize_playlist_stem(name: str) -> str:
    """Same sanitization generate_m3u uses — exposed so callers can compute keep_names."""
    stem = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    return stem or "playlist"
