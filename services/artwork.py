import io
import logging

import httpx
from PIL import Image

from config import settings

logger = logging.getLogger(__name__)


async def fetch_artwork(url: str) -> bytes | None:
    """Download the raw cover image; None if it cannot be fetched."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, follow_redirects=True, timeout=15)
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.warning("Failed to download artwork from %s: %s", url, e)
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
