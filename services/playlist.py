"""
M3U playlist generation for HIFI WALKER H2.

H2 playlist rules (stock firmware):
- Playlist files live at the ROOT of the SD card
- Paths inside are RELATIVE to the SD card root (no leading slash)
- Use forward slashes for path separators
- .m3u8 extension + UTF-8 BOM so non-ASCII filenames resolve
- CRLF line endings for FAT32 / embedded-player compatibility

On a Rockbox card playlists go to /Playlists (Rockbox's playlist catalog) as UTF-8 without BOM,
starting with #EXTM3U, LF endings, and entries absolute from the card root: Rockbox resolves a
relative entry against the playlist's own folder.
"""
import json
import logging
import os
from pathlib import Path, PurePosixPath

from services.device import CARD_PROFILE_ROCKBOX, CARD_PROFILE_STOCK, build_device_path, is_link, is_protected_card_path

logger = logging.getLogger(__name__)

PLAYLIST_EXT = ".m3u8"
PLAYLIST_DIR_ROCKBOX = "Playlists"
_PLAYLIST_SUFFIXES = (".m3u", ".m3u8")


class ForeignPlaylistError(FileExistsError):
    """The playlist's file on the card is not one Leasure wrote, so it is never overwritten."""


def playlist_dir(profile: str) -> str:
    """The card folder Leasure writes playlists to: "" (the root) or "Playlists"."""
    return PLAYLIST_DIR_ROCKBOX if profile == CARD_PROFILE_ROCKBOX else ""


def prepare_playlist_dir(device_root: Path, profile: str) -> Path:
    """The folder this profile's playlists go to, created when missing. Raises NotADirectoryError
    when /Playlists is a file or a link: it is never replaced."""
    folder = playlist_dir(profile)
    if not folder:
        return device_root
    path = device_root / folder
    if is_link(path) or (path.exists() and not path.is_dir()):
        raise NotADirectoryError(f"/{folder} is not a folder")
    path.mkdir(exist_ok=True)
    return path


def _playlist_bytes(entries: list[str], profile: str) -> bytes:
    if profile == CARD_PROFILE_ROCKBOX:
        return ("#EXTM3U\n" + "".join(f"/{e}\n" for e in entries)).encode("utf-8")
    return b"\xef\xbb\xbf" + ("\r\n".join(entries) + "\r\n").encode("utf-8")


def generate_m3u(playlist_name: str, tracks: list[dict], device_root: Path, profile: str = CARD_PROFILE_STOCK) -> Path:
    """Generate a playlist file at the root of the SD card, or in /Playlists on a Rockbox card.
    Raises ForeignPlaylistError when /Playlists already holds a file of that name Leasure did not write."""
    m3u_path = prepare_playlist_dir(device_root, profile) / f"{sanitize_playlist_stem(playlist_name)}{PLAYLIST_EXT}"
    # /Playlists is Rockbox's own catalog, where the player saves the user's playlists.
    if profile == CARD_PROFILE_ROCKBOX and not _ours_or_absent(device_root, m3u_path):
        raise ForeignPlaylistError(f"/{m3u_path.relative_to(device_root).as_posix()} is not one of Leasure's playlists")

    lines = []
    for track in tracks:
        if track.get("rel_path"):
            lines.append(track["rel_path"])
            continue
        ext = track.get("format", "mp3")
        rel_path = build_device_path(
            track["artist"],
            track.get("album", ""),
            track.get("track_number"),
            track["title"],
            ext,
            album_artist=track.get("album_artist"),
            disc_number=track.get("disc_number"),
            total_discs=track.get("total_discs"),
        )
        lines.append(rel_path)

    m3u_path.write_bytes(_playlist_bytes(lines, profile))
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
    """Card-root-relative POSIX paths of the playlist files Leasure itself wrote to this card on
    earlier syncs: bare names at the root, "Playlists/<name>" on a Rockbox card."""
    path = _manifest_path(device_root)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(n) for n in data.get("files", [])}
    except (OSError, ValueError) as e:
        logger.warning("Unreadable playlist manifest %s: %s", path, e)
        return set()


def _ours_or_absent(device_root: Path, path: Path) -> bool:
    if not os.path.lexists(path):
        return True
    rel = path.relative_to(device_root).as_posix().casefold()
    return rel in {n.casefold() for n in read_manifest(device_root)}


def write_manifest(device_root: Path, files: set[str]) -> None:
    path = _manifest_path(device_root)
    try:
        path.write_text(json.dumps({"version": 1, "files": sorted(files)}, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write playlist manifest %s: %s", path, e)


def _manifest_entry_ok(device_root: Path, rel: str) -> bool:
    """A manifest entry that may be deleted: a regular playlist file at the root or directly in a real
    /Playlists folder, as Leasure writes them. Anything else in the manifest is never touched."""
    if "\\" in rel or ":" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or len(parts) > 2:
        return False
    if len(parts) == 2 and (parts[0] != PLAYLIST_DIR_ROCKBOX or is_link(device_root / parts[0])):
        return False
    if PurePosixPath(rel).suffix.lower() not in _PLAYLIST_SUFFIXES or is_protected_card_path(rel):
        return False
    path = device_root / rel
    return path.is_file() and not is_link(path)


def _superseded_location(device_root: Path, rel: str, keep_names: set[str], current_dir: str, ours: set[str]) -> bool:
    """A kept playlist recorded in the other profile's folder, whose current-profile copy Leasure wrote."""
    entry = PurePosixPath(rel)
    folder = "" if entry.parent == PurePosixPath(".") else entry.parent.as_posix()
    if entry.stem not in keep_names or folder == current_dir:
        return False
    current = (PurePosixPath(current_dir) / f"{entry.stem}{PLAYLIST_EXT}").as_posix()
    return current in ours and (device_root / current).is_file()


def sweep_orphan_playlists(device_root: Path, keep_names: set[str], profile: str = CARD_PROFILE_STOCK) -> list[str]:
    """
    Delete playlist files *that Leasure wrote on a previous sync* and that no longer
    correspond to a DB playlist, or that the card's profile moved (root <-> /Playlists) once
    the new copy exists. Playlists the user put on the card by hand are never touched: only
    entries recorded in the card's manifest are candidates. /Playlists itself is never removed.

    `keep_names` must contain the sanitized stems that generate_m3u would produce.
    """
    ours = read_manifest(device_root)
    current_dir = playlist_dir(profile)
    removed = []
    for rel in sorted(ours):
        if not _manifest_entry_ok(device_root, rel):
            continue
        if PurePosixPath(rel).stem in keep_names and not _superseded_location(device_root, rel, keep_names, current_dir, ours):
            continue
        try:
            (device_root / rel).unlink()
            removed.append(rel)
        except OSError as e:
            logger.warning("Failed to remove orphan playlist %s: %s", rel, e)
    if removed:
        write_manifest(device_root, ours - set(removed))
    return removed


def record_generated_playlists(device_root: Path, paths: list[Path]) -> None:
    """Remember which playlist files this sync wrote so a later sweep can reclaim them."""
    ours = read_manifest(device_root)
    ours.update(p.relative_to(device_root).as_posix() for p in paths)
    write_manifest(device_root, ours)


def sanitize_playlist_stem(name: str) -> str:
    """Same sanitization generate_m3u uses — exposed so callers can compute keep_names."""
    stem = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    return stem or "playlist"
