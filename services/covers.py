"""Per-album cover.jpg on Rockbox cards. Rockbox shows the cover.jpg of the album folder and cannot
decode progressive JPEG, so each cover is re-encoded baseline from the album's sidecar art.

Leasure records the covers it wrote in /.leasure-covers.json and only replaces or removes a cover
whose bytes are still the ones it recorded. A cover.jpg it did not write (any case: FAT is
case-insensitive) is never touched. An unreadable manifest means nothing is Leasure's.
All functions here block: call them in a worker thread."""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from config import settings
from services import artwork
from services.device import (
    inside_real_dirs,
    is_link,
    is_protected_card_path,
    remove_empty_card_dirs,
    safe_card_rel,
)

logger = logging.getLogger(__name__)

COVERS_MANIFEST = ".leasure-covers.json"
COVER_NAME = "cover.jpg"
MAX_COVER_PX = 500
_PART_SUFFIX = ".leasure-part"
# FAT keeps mtimes in 2-second steps, in local time (an hour off across a DST change).
_MTIME_TOLERANCE_S = 2


@dataclass
class CoverResult:
    written: int = 0
    replaced: int = 0
    kept_user: int = 0
    errors: list[str] = field(default_factory=list)


def read_cover_manifest(target: Path) -> dict[str, dict]:
    path = target / COVERS_MANIFEST
    if not path.exists():
        return {}
    try:
        covers = json.loads(path.read_text(encoding="utf-8")).get("covers")
    except (OSError, ValueError, AttributeError) as e:
        logger.warning("Unreadable cover manifest %s: %s", path, e)
        return {}
    if not isinstance(covers, dict):
        return {}
    return {str(rel): rec for rel, rec in covers.items() if isinstance(rec, dict)}


def _write_cover_manifest(target: Path, records: dict[str, dict]) -> None:
    path = target / COVERS_MANIFEST
    part = path.with_name(path.name + _PART_SUFFIX)
    part.write_text(json.dumps({"version": 1, "covers": dict(sorted(records.items()))}, indent=1), encoding="utf-8")
    os.replace(part, path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cover_rel_ok(target: Path, rel: str) -> bool:
    return (safe_card_rel(rel) and PurePosixPath(rel).name == COVER_NAME and not is_protected_card_path(rel)
            and inside_real_dirs(target, rel))


def _find_cover(folder: Path) -> Path | None:
    """The folder's cover.jpg in whatever case it was written; the exact name wins."""
    exact = folder / COVER_NAME
    if os.path.lexists(exact):
        return exact
    return next((p for p in folder.iterdir() if p.name.casefold() == COVER_NAME), None)


def _unchanged(path: Path, record: dict) -> bool:
    st = path.stat()
    mtime = record.get("mtime")
    if st.st_size != record.get("size") or not isinstance(mtime, int | float):
        return False
    return any(abs(st.st_mtime - mtime - k * 3600) <= _MTIME_TOLERANCE_S for k in (-1, 0, 1))


def _write_atomic(path: Path, data: bytes) -> None:
    part = path.with_name(f".{path.name}{_PART_SUFFIX}")
    try:
        part.write_bytes(data)
        os.replace(part, path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _record(path: Path, data: bytes, source: list[int]) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": st.st_mtime, "sha256": hashlib.sha256(data).hexdigest(), "source": source}


def _write_album_cover(target: Path, album_dir: str, sidecar: Path, records: dict[str, dict],
                       result: CoverResult) -> None:
    rel = f"{album_dir}/{COVER_NAME}"
    folder = target / album_dir
    if not _cover_rel_ok(target, rel) or not folder.is_dir():
        return
    existing = _find_cover(folder)
    record = records.get(rel)
    src_stat = sidecar.stat()
    source = [src_stat.st_size, src_stat.st_mtime_ns]
    if existing is not None and record and record.get("source") == source and _unchanged(existing, record):
        return
    if existing is not None and (record is None or is_link(existing) or not existing.is_file()
                                 or _sha256(existing) != record.get("sha256")):
        if records.pop(rel, None) is not None:
            logger.info("cover.jpg in %s changed on the card; Leasure leaves it alone from now on", album_dir)
        result.kept_user += 1
        return
    data = artwork.prepare_cover(sidecar.read_bytes(), min(settings.artwork_size, MAX_COVER_PX))
    if data is None:
        return
    if existing is not None and hashlib.sha256(data).hexdigest() == record["sha256"]:
        st = existing.stat()
        records[rel] = {**record, "size": st.st_size, "mtime": st.st_mtime, "source": source}
        return
    path = existing or folder / COVER_NAME
    _write_atomic(path, data)
    records[rel] = _record(path, data, source)
    if existing is None:
        result.written += 1
    else:
        result.replaced += 1


def write_album_covers(target: Path, albums: dict[str, Path]) -> CoverResult:
    """Write or refresh Leasure's cover.jpg in each album folder (`albums`: album-folder rel -> the
    library sidecar art to make it from). Only for Rockbox cards."""
    records = read_cover_manifest(target)
    before = json.dumps(records, sort_keys=True)
    result = CoverResult()
    for album_dir, sidecar in albums.items():
        try:
            _write_album_cover(target, album_dir, sidecar, records, result)
        except OSError as e:
            logger.warning("Could not write cover.jpg in %s: %s", album_dir, e)
            result.errors.append(f"cover.jpg for {album_dir}: {e}")
    if json.dumps(records, sort_keys=True) != before:
        _write_cover_manifest(target, records)
    return result


def _sweep_one(target: Path, rel: str, record: dict) -> str:
    """"removed", "dropped" (the record goes, the file stays) or "kept" (record and file stay)."""
    if not _cover_rel_ok(target, rel):
        return "kept"
    path = target / rel
    if not os.path.lexists(path):
        return "dropped"
    if is_link(path) or not path.is_file() or _sha256(path) != record.get("sha256"):
        return "dropped"
    path.unlink()
    remove_empty_card_dirs(target, path.parent)
    return "removed"


def sweep_album_covers(target: Path, live_album_dirs: set[str]) -> list[str]:
    """Remove Leasure's covers from album folders no library track maps to (`live_album_dirs`,
    casefolded), with the folders they leave empty. Runs whatever the card's profile is."""
    if not (target / COVERS_MANIFEST).exists():
        return []
    records = read_cover_manifest(target)
    removed, changed = [], False
    for rel, record in sorted(records.items()):
        if PurePosixPath(rel).parent.as_posix().casefold() in live_album_dirs:
            continue
        try:
            outcome = _sweep_one(target, rel, record)
        except OSError as e:
            logger.warning("Could not remove the cover %s: %s", rel, e)
            continue
        if outcome != "kept":
            del records[rel]
            changed = True
        if outcome == "removed":
            removed.append(rel)
    if changed:
        _write_cover_manifest(target, records)
    return removed
