"""Deezer's public catalogue API (no account): album search, album facts and tracklists.

Deezer allows 50 requests per 5 s; Leasure stays near one per second. Errors arrive as
HTTP 200 with an `error` object: quota (4) and busy (700) are retried once after 5 s,
"no data" (800) is an empty answer, anything else is DeezerUnavailable."""

import asyncio
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from services.musicbrainz import USER_AGENT

logger = logging.getLogger(__name__)

BASE_URL = "https://api.deezer.com"
TIMEOUT = 10.0
MIN_INTERVAL = 1.0
RETRY_AFTER = 5.0
BUSY_CODES = frozenset({4, 700})
NO_DATA_CODE = 800
SEARCH_LIMIT = 25
PAGE_SIZE = 100
MAX_PAGES = 5

_rate_lock = asyncio.Lock()
_last_request = 0.0


class DeezerUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(f"Deezer unavailable ({detail})")
        self.detail = detail


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _throttle() -> None:
    global _last_request
    async with _rate_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            await _sleep(wait)
        _last_request = time.monotonic()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


async def get_checked(path: str, params: dict[str, Any]) -> dict | None:
    """One throttled request; None when Deezer has no data for it."""
    async with _client() as client:
        for attempt in (0, 1):
            await _throttle()
            try:
                resp = await client.get(f"{BASE_URL}{path}", params=params)
                data = resp.json() if resp.status_code == 200 else None
            except (httpx.HTTPError, ValueError) as e:
                raise DeezerUnavailable(e.__class__.__name__) from None
            if resp.status_code != 200:
                raise DeezerUnavailable(f"HTTP {resp.status_code}")
            error = data.get("error") if isinstance(data, dict) else None
            if not error:
                return data if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code in BUSY_CODES and attempt == 0:
                await _sleep(RETRY_AFTER)
                continue
            if code == NO_DATA_CODE:
                return None
            raise DeezerUnavailable(f"error {code}")
    raise DeezerUnavailable("busy")


def _quoted(text: str) -> str:
    return " ".join((text or "").replace('"', " ").split())


async def search_albums(artist: str, album: str) -> list[dict]:
    query = f'artist:"{_quoted(artist)}" album:"{_quoted(album)}"'
    data = await get_checked("/search/album", {"q": query, "limit": SEARCH_LIMIT})
    return list((data or {}).get("data") or [])


async def album(album_id: str) -> dict | None:
    return await get_checked(f"/album/{album_id}", {})


def _next_index(next_url: str | None) -> int | None:
    if not next_url:
        return None
    try:
        return int(parse_qs(urlsplit(next_url).query)["index"][0])
    except (KeyError, IndexError, ValueError):
        return None


async def album_tracks(album_id: str, nb_tracks: int | None) -> list[dict]:
    """Every track of the album, following `next` for at most MAX_PAGES pages."""
    tracks: list[dict] = []
    index: int | None = 0
    for _ in range(MAX_PAGES):
        data = await get_checked(f"/album/{album_id}/tracks", {"limit": PAGE_SIZE, "index": index})
        tracks += list((data or {}).get("data") or [])
        index = _next_index((data or {}).get("next"))
        if index is None or (nb_tracks and len(tracks) >= nb_tracks):
            break
    return tracks
