"""Same-origin copy of provider cover art whose CDN sends no CORS header (www.gstatic.com),
so the WebGL screen can still read it as a texture."""
import asyncio
import logging
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_HOSTS = frozenset({
    "www.gstatic.com",
    "i.scdn.co",
    "mosaic.scdn.co",
    "yt3.googleusercontent.com",
    "lh3.googleusercontent.com",
    "i.ytimg.com",
})
SPOTIFY_IMAGE_CDN = re.compile(r"image-cdn-[a-z0-9]+\.spotifycdn\.com")
# No SVG: served from our origin it could run script.
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 10.0


def _allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    return (parts.scheme == "https" and port in (None, 443) and not parts.username and not parts.password
            and (host in ALLOWED_HOSTS or SPOTIFY_IMAGE_CDN.fullmatch(host) is not None))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)


async def _fetch_image(url: str) -> tuple[bytes, str]:
    async with _client() as client, client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise ValueError(f"upstream answered {resp.status_code}")
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in IMAGE_TYPES:
            raise ValueError(f"upstream sent {content_type or 'no content type'}")
        if int(resp.headers.get("content-length") or 0) > MAX_BYTES:
            raise ValueError("image too large")
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError("image too large")
    return bytes(body), content_type


@router.get("/proxy")
async def proxy_art(url: str):
    if len(url) > 2048 or not _allowed(url):
        raise HTTPException(400, detail="Only https cover art from Spotify or YouTube image hosts can be proxied.")
    try:
        async with asyncio.timeout(TIMEOUT_S):
            body, content_type = await _fetch_image(url)
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        logger.warning("Cover proxy failed for %s: %s", url, e)
        raise HTTPException(502, detail="The image host did not return a usable image.") from None
    return Response(body, media_type=content_type, headers={
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    })
