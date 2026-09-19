import logging
import re
from pathlib import Path

import httpx

from services.tagger import primary_artist

logger = logging.getLogger(__name__)

LRCLIB_API = "https://lrclib.net/api"
USER_AGENT = "Leasure/0.1.0 (https://github.com/joseandresv/leasure)"
DURATION_TOLERANCE_S = 3


def _normalised(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text or "").casefold().split())


def _hit_matches(hit: dict, title: str, artist: str, duration_s: int) -> bool:
    """Search hits are ranked by fuzzy text alone, so a wrong song is a likely top
    result; only accept one whose length, title and artist all line up."""
    if not duration_s or abs((hit.get("duration") or 0) - duration_s) > DURATION_TOLERANCE_S:
        return False
    if _normalised(hit.get("trackName", "")) != _normalised(title):
        return False
    return bool(artist) and _normalised(artist) in _normalised(hit.get("artistName", ""))


async def fetch_lrc(title: str, artist: str, album: str = "", duration_s: int = 0) -> str | None:
    """Synced lyrics from lrclib.net for a track that matches exactly, else None."""
    lead = primary_artist(artist) or artist
    try:
        params = {"track_name": title, "artist_name": lead}
        if album:
            params["album_name"] = album
        if duration_s:
            params["duration"] = str(duration_s)

        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(f"{LRCLIB_API}/get", params=params, timeout=10)
            if resp.status_code == 200:
                synced = resp.json().get("syncedLyrics")
                if synced:
                    logger.info("Found synced lyrics for '%s' by '%s'", title, lead)
                    return synced

            resp = await client.get(
                f"{LRCLIB_API}/search",
                params={"q": f"{lead} {title}"},
                timeout=10,
            )
            if resp.status_code == 200:
                for hit in resp.json() or []:
                    if hit.get("syncedLyrics") and _hit_matches(hit, title, lead, duration_s):
                        logger.info("Found synced lyrics via search for '%s'", title)
                        return hit["syncedLyrics"]

    except Exception as e:
        logger.debug("Lyrics fetch failed for '%s': %s", title, e)

    return None


async def save_lrc(audio_path: Path, title: str, artist: str, album: str = "", duration_ms: int = 0):
    """Fetch and save .lrc file alongside the audio file."""
    lrc_path = audio_path.with_suffix(".lrc")
    if lrc_path.exists():
        return

    duration_s = duration_ms // 1000 if duration_ms else 0
    lrc_content = await fetch_lrc(title, artist, album, duration_s)

    if lrc_content:
        lrc_path.write_text(lrc_content, encoding="utf-8")
        logger.info("Saved lyrics to %s", lrc_path)
