import io
import logging
from pathlib import Path

import httpx
from PIL import Image

from config import settings

logger = logging.getLogger(__name__)


async def download_and_save_artwork(url: str, output_path: Path, size: int | None = None) -> bool:
    size = size or settings.artwork_size
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()

        img = Image.open(io.BytesIO(resp.content))
        img = img.resize((size, size), Image.LANCZOS)
        img.convert("RGB").save(output_path, "JPEG", quality=90)
        logger.info("Saved artwork to %s", output_path)
        return True
    except Exception as e:
        logger.warning("Failed to download artwork from %s: %s", url, e)
        return False
