"""
M3U playlist generation for HIFI WALKER H2.

H2 playlist rules (from user manual + Reddit):
- Supports .m3u and .m3u8 files
- Playlist files MUST be at the ROOT of the SD card
- Music files can be anywhere on the card
- Paths are relative to the SD card root
- Playlists show up under Explorer, not the Playlists category
- Keep format simple — just file paths, one per line
- Use forward slashes for path separators
"""
import logging
from pathlib import Path

from services.device import build_device_path

logger = logging.getLogger(__name__)


def generate_m3u(playlist_name: str, tracks: list[dict], device_root: Path) -> Path:
    """
    Generate an .m3u playlist file at the root of the SD card.

    Uses simple M3U format (just paths) for maximum H2 compatibility.
    """
    # Sanitize playlist name for filename
    safe_name = "".join(c for c in playlist_name if c not in r'\/:*?"<>|').strip()
    if not safe_name:
        safe_name = "playlist"

    m3u_path = device_root / f"{safe_name}.m3u"

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
        # H2 expects absolute paths from SD card root with leading slash
        lines.append(f"/{rel_path}")

    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Generated playlist '%s' with %d tracks at %s", playlist_name, len(tracks), m3u_path)
    return m3u_path


def generate_all_playlists(playlists: dict[str, list[dict]], device_root: Path) -> list[Path]:
    """Generate multiple .m3u playlists at the root of the SD card."""
    generated = []
    for name, tracks in playlists.items():
        if tracks:
            path = generate_m3u(name, tracks, device_root)
            generated.append(path)
    return generated
