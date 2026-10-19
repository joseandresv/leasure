"""Spotify album tracklists kept under data/: they do not change, and Spotify may rate-limit Leasure for hours."""

import json
import logging
import re
import time
from pathlib import Path

from config import settings
from services import secret_file
from services import spotify_client as sp

logger = logging.getLogger(__name__)

FRESH_FOR_S = 30 * 86400
_ALBUM_ID = re.compile(r"[0-9A-Za-z]{22}")


def cache_dir() -> Path:
    return settings.data_dir / "spotify_albums"


def is_album_id(album_id: str) -> bool:
    return bool(_ALBUM_ID.fullmatch(album_id or ""))


def _read(path: Path) -> dict | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable Spotify album cache %s: %s", path.name, e)
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get("data"), dict) else None


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_atomic(path, json.dumps({"fetched_at": time.time(), "data": data}))
    except OSError as e:
        logger.warning("Could not cache Spotify album %s: %s", path.stem, e)


def _is_fresh(entry: dict) -> bool:
    age = time.time() - (entry.get("fetched_at") or 0)
    # An entry saved while the ISRC lookup was refused is refetched, so Qobuz can match by ISRC later.
    has_isrcs = any(t.get("isrc") for t in entry["data"].get("tracks") or [])
    return 0 <= age < FRESH_FOR_S and has_isrcs


def get_album(album_id: str) -> dict | None:
    """sp.get_album_tracks() through the cache; a stale entry stands in whenever Spotify cannot answer."""
    if not is_album_id(album_id):
        return None
    path = cache_dir() / f"{album_id}.json"
    entry = _read(path)
    if entry and _is_fresh(entry):
        return entry["data"]
    data = None
    if not sp.is_rate_limited():
        try:
            data = sp.get_album_tracks(album_id)
        except Exception as e:
            logger.warning("Spotify album %s lookup failed: %s", album_id, e)
    if data:
        _write(path, data)
        return data
    return entry["data"] if entry else None
