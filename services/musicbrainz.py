"""MusicBrainz genre lookup: rate-limited, disk-cached, score-gated.

MusicBrainz asks for an identifying User-Agent and at most one request per second;
it answers 503 when that budget is exceeded. Genres (curated) are used, never the
raw community `tags`, and a search hit is only trusted above MIN_SCORE with a
matching title, so a fuzzy match cannot mislabel a track.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

VERSION = "0.1.0"
USER_AGENT = f"Leasure/{VERSION} (https://github.com/joseandresv/leasure)"
BASE_URL = "https://musicbrainz.org/ws/2"
TIMEOUT = 10.0
MIN_INTERVAL = 1.1
RETRY_AFTER = 2.0
MIN_SCORE = 90
MAX_GENRES = 3
HIT_TTL = timedelta(days=30)
MISS_TTL = timedelta(days=7)
RECORDING_INC = "genres+artist-credits+release-groups"
# The isrc resource rejects genres/release-groups with HTTP 400; it only browses to recordings.
ISRC_INC = "artist-credits"

_LUCENE_SPECIALS = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

_rate_lock = asyncio.Lock()
_last_request = 0.0
_cache: dict[str, Any] | None = None


async def genres_for_track(*, isrc: str | None, title: str, artist: str, album: str | None = None) -> list[str]:
    """Up to three MusicBrainz genres for a track, most-voted first; [] when nothing matches."""
    isrc = (isrc or "").strip().upper()
    primary = (artist or "").split(",")[0].strip()
    title = (title or "").strip()
    if not isrc and not primary:
        return []

    key = f"isrc:{isrc}" if isrc else f"track:{_normalise(primary)}|{_normalise(title)}|{_normalise(album)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        genres: list[str] = []
        if isrc:
            genres = await _genres_by_isrc(isrc)
        if not genres and title and primary:
            genres = await _genres_by_recording_search(title, primary, album)
        if not genres and primary:
            genres = await _genres_by_artist_search(primary)
    except Exception as e:
        logger.debug("MusicBrainz lookup failed for '%s' / '%s': %s", artist, title, e)
        return []

    _cache_put(key, genres)
    return genres


async def get(path: str, params: dict[str, Any]) -> dict | None:
    """One MusicBrainz request under the app's shared User-Agent and rate limit; None on any non-200 answer."""
    return await _get(path, params)


async def _genres_by_isrc(isrc: str) -> list[str]:
    data = await _get(f"/isrc/{isrc}", {"inc": ISRC_INC})
    for stub in (data or {}).get("recordings") or []:
        if not stub.get("id"):
            continue
        recording = await _get(f"/recording/{stub['id']}", {"inc": RECORDING_INC})
        genres = _genres_of_recording(recording or {})
        if genres:
            return genres
    return []


async def _genres_by_recording_search(title: str, artist: str, album: str | None) -> list[str]:
    query = f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"'
    data = await _get("/recording/", {"query": query, "limit": 3})
    hits = (data or {}).get("recordings") or []
    if not hits or _score(hits[0]) < MIN_SCORE or not _same(hits[0].get("title"), title):
        return []

    hit = _hit_for_album(hits, album) or hits[0]
    recording = await _get(f"/recording/{hit['id']}", {"inc": RECORDING_INC})
    return _genres_of_recording(recording or {})


async def _genres_by_artist_search(artist: str) -> list[str]:
    data = await _get("/artist/", {"query": f'artist:"{_lucene(artist)}"', "limit": 1})
    artists = (data or {}).get("artists") or []
    if not artists or _score(artists[0]) < MIN_SCORE:
        return []
    return await _genres_of_artist(artists[0]["id"])


def _genres_of_recording(recording: dict) -> list[str]:
    genres = _rank_genres([recording, *_release_groups(recording)])
    if genres:
        return genres
    return _rank_genres([credit.get("artist") or {} for credit in recording.get("artist-credit") or []])


async def _genres_of_artist(artist_id: str) -> list[str]:
    data = await _get(f"/artist/{artist_id}", {"inc": "genres"})
    return _rank_genres([data or {}])


def _rank_genres(objects: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    for obj in objects:
        for genre in obj.get("genres") or []:
            name = (genre.get("name") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + int(genre.get("count") or 0)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ranked[:MAX_GENRES]]


def _release_groups(recording: dict) -> list[dict]:
    groups = list(recording.get("release-groups") or [])
    for release in recording.get("releases") or []:
        group = release.get("release-group")
        if group:
            groups.append(group)
    return groups


def _hit_for_album(hits: list[dict], album: str | None) -> dict | None:
    if not album:
        return None
    for hit in hits:
        if _score(hit) < MIN_SCORE:
            continue
        titles = [r.get("title") for r in hit.get("releases") or []]
        titles += [g.get("title") for g in _release_groups(hit)]
        if any(_same(t, album) for t in titles):
            return hit
    return None


def _score(hit: dict) -> int:
    try:
        return int(hit.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _lucene(text: str) -> str:
    return _LUCENE_SPECIALS.sub(r"\\\1", text)


def _normalise(text: str | None) -> str:
    return re.sub(r"\W+", "", text or "", flags=re.UNICODE).casefold()


def _same(left: str | None, right: str | None) -> bool:
    left, right = _normalise(left), _normalise(right)
    return bool(left) and left == right


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _throttle() -> None:
    global _last_request
    async with _rate_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            await _sleep(wait)
        _last_request = time.monotonic()


async def _get(path: str, params: dict[str, Any]) -> dict | None:
    """One rate-limited MusicBrainz request; None on any non-200 answer."""
    query = {**params, "fmt": "json"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        for attempt in (0, 1):
            await _throttle()
            resp = await client.get(f"{BASE_URL}{path}", params=query)
            if resp.status_code == 503 and attempt == 0:
                await _sleep(RETRY_AFTER)
                continue
            if resp.status_code != 200:
                logger.debug("MusicBrainz %s answered HTTP %s", path, resp.status_code)
                return None
            return resp.json()
    return None


def _cache_path() -> Path:
    return settings.data_dir / "musicbrainz_cache.json"


def _load_cache() -> dict[str, Any]:
    global _cache
    if _cache is None:
        _cache = {}
        try:
            loaded = json.loads(_cache_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            loaded = None
        except (OSError, ValueError) as e:
            logger.debug("Ignoring unreadable MusicBrainz cache: %s", e)
            loaded = None
        if isinstance(loaded, dict):
            _cache = loaded
    return _cache


def _cache_get(key: str) -> list[str] | None:
    entry = _load_cache().get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("genres"), list):
        return None
    try:
        at = datetime.fromisoformat(entry.get("at", ""))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    genres = [str(g) for g in entry["genres"]]
    if datetime.now(UTC) - at > (HIT_TTL if genres else MISS_TTL):
        return None
    return genres


def _cache_put(key: str, genres: list[str]) -> None:
    cache = _load_cache()
    cache[key] = {"genres": genres, "at": datetime.now(UTC).isoformat()}
    path = _cache_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Could not write MusicBrainz cache %s: %s", path, e)
