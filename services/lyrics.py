import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

LRCLIB_API = "https://lrclib.net/api"


async def fetch_lrc(title: str, artist: str, album: str = "", duration_s: int = 0) -> str | None:
    """Fetch synced lyrics from lrclib.net. Returns LRC content or None."""
    try:
        params = {
            "track_name": title,
            "artist_name": artist,
        }
        if album:
            params["album_name"] = album
        if duration_s:
            params["duration"] = str(duration_s)

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{LRCLIB_API}/get", params=params, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                # Prefer synced lyrics, fall back to plain
                lrc = data.get("syncedLyrics") or data.get("plainLyrics")
                if lrc:
                    logger.info("Found lyrics for '%s' by '%s'", title, artist)
                    return lrc

            # Try search as fallback
            resp = await client.get(
                f"{LRCLIB_API}/search",
                params={"q": f"{artist} {title}"},
                timeout=10,
            )
            if resp.status_code == 200:
                results = resp.json()
                if results:
                    lrc = results[0].get("syncedLyrics") or results[0].get("plainLyrics")
                    if lrc:
                        logger.info("Found lyrics via search for '%s'", title)
                        return lrc

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
