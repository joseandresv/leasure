import io
import logging
from urllib.parse import urlsplit

import httpx
from PIL import Image

from config import settings

logger = logging.getLogger(__name__)


MAX_ARTWORK_BYTES = 5 * 1024 * 1024
# Qobuz cover URLs are checked at the source; a redirect could leave that host.
NO_REDIRECT_HOSTS = frozenset({"static.qobuz.com"})


def _client(follow_redirects: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=follow_redirects, timeout=15)


async def _read_image(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise ValueError(f"not an image ({content_type or 'no content type'})")
        if int(resp.headers.get("content-length") or 0) > MAX_ARTWORK_BYTES:
            raise ValueError("image too large")
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > MAX_ARTWORK_BYTES:
                raise ValueError("image too large")
    return bytes(body)


async def fetch_artwork(url: str) -> bytes | None:
    """Download the raw cover image; None if it cannot be fetched."""
    host = ""
    try:
        host = urlsplit(url).hostname or ""
        async with _client(follow_redirects=host not in NO_REDIRECT_HOSTS) as client:
            return await _read_image(client, url)
    except Exception as e:
        # The host only: provider image URLs can carry signed query strings.
        logger.warning("Failed to download artwork from %s: %s", host or "an unparsable URL", e.__class__.__name__)
        return None


def prepare_cover(data: bytes, size: int | None = None) -> bytes | None:
    """Centre-cropped square JPEG, scaled down to `size` but never up: a small
    thumbnail stays at its native size instead of being blown up and blurred."""
    size = size or settings.artwork_size
    try:
        img = Image.open(io.BytesIO(data))
        side = min(img.width, img.height)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
        if side > size:
            img = img.resize((size, size), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        logger.warning("Unusable artwork image (%d bytes): %s", len(data), e)
        return None
