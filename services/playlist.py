"""
M3U playlist generation for HIFI WALKER H2.

H2 playlist rules:
- Playlist files live at the ROOT of the SD card
- Paths inside are RELATIVE to the SD card root (no leading slash)
- Use forward slashes for path separators
- .m3u8 extension + UTF-8 BOM so non-ASCII filenames resolve
- CRLF line endings for FAT32 / embedded-player compatibility
"""
import json
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


MANIFEST_NAME = ".leasure-playlists.json"


def _manifest_path(device_root: Path) -> Path:
    return device_root / MANIFEST_NAME


def read_manifest(device_root: Path) -> set[str]:
    """Names of the playlist files Leasure itself wrote to this card on earlier syncs."""
    path = _manifest_path(device_root)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(n) for n in data.get("files", [])}
    except (OSError, ValueError) as e:
        logger.warning("Unreadable playlist manifest %s: %s", path, e)
        return set()


def write_manifest(device_root: Path, files: set[str]) -> None:
    path = _manifest_path(device_root)
    try:
        path.write_text(json.dumps({"version": 1, "files": sorted(files)}, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write playlist manifest %s: %s", path, e)


def sweep_orphan_playlists(device_root: Path, keep_names: set[str]) -> list[str]:
    """
    Delete playlist files *that Leasure wrote on a previous sync* and that no longer
    correspond to a DB playlist. Playlists the user put on the card by hand are never
    touched: only names recorded in the card's manifest are candidates.

    `keep_names` must contain the sanitized stems that generate_m3u would produce.
    """
    ours = read_manifest(device_root)
    removed = []
    for name in sorted(ours):
        f = device_root / name
        if f.stem in keep_names:
            continue
        if not f.is_file() or f.suffix.lower() not in (".m3u", ".m3u8"):
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as e:
            logger.warning("Failed to remove orphan playlist %s: %s", f, e)
    if removed:
        write_manifest(device_root, ours - set(removed))
    return removed


def record_generated_playlists(device_root: Path, paths: list[Path]) -> None:
    """Remember which playlist files this sync wrote so a later sweep can reclaim them."""
    ours = read_manifest(device_root)
    ours.update(p.name for p in paths)
    write_manifest(device_root, ours)


def sanitize_playlist_stem(name: str) -> str:
    """Same sanitization generate_m3u uses — exposed so callers can compute keep_names."""
    stem = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    return stem or "playlist"
