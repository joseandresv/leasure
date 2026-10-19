import asyncio
import hashlib
import io
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image
from sqlalchemy import select

from config import settings

logger = logging.getLogger(__name__)


MAX_ARTWORK_BYTES = 5 * 1024 * 1024
# Qobuz cover URLs are checked at the source; a redirect could leave that host.
NO_REDIRECT_HOSTS = frozenset({"static.qobuz.com"})
_LIBRARY_ART_URL = re.compile(r"/api/library/art/(\d+)")
_ALBUM_COVER_URL = re.compile(r"/api/albums/(a-[0-9a-f]{20})/cover(?:\?.*)?")


def sidecar_art_path(file_path: str | None) -> Path | None:
    """The track's existing .jpg sidecar, only if it resolves inside the library dir. Blocking."""
    if not file_path:
        return None
    try:
        jpg = Path(file_path).with_suffix(".jpg").resolve()
    except (OSError, ValueError):
        return None
    if not jpg.is_relative_to(settings.library_dir.resolve()) or not jpg.is_file():
        return None
    return jpg


def library_art_url(track_id: int) -> str:
    return f"/api/library/art/{track_id}"


def album_cover_path(album_key: str) -> Path:
    """Where the album view keeps a cover fetched for an album none of whose songs has a sidecar."""
    return settings.data_dir / "album-covers" / f"{album_key}.jpg"


async def local_cover_path(url: str) -> Path | None:
    """The file behind one of the app's own cover URLs (a track's sidecar or an album cover), if it exists."""
    if match := _LIBRARY_ART_URL.fullmatch(url):
        from db import async_session
        from models import Track

        async with async_session() as session:
            file_path = await session.scalar(select(Track.file_path).where(Track.id == int(match[1])))
        return await asyncio.to_thread(sidecar_art_path, file_path)
    if match := _ALBUM_COVER_URL.fullmatch(url):
        path = album_cover_path(match[1])
        return path if await asyncio.to_thread(path.is_file) else None
    return None


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
        # Baseline, not progressive: Rockbox cannot decode progressive JPEG.
        img.convert("RGB").save(buf, "JPEG", quality=90, progressive=False, optimize=False)
        return buf.getvalue()
    except Exception as e:
        logger.warning("Unusable artwork image (%d bytes): %s", len(data), e)
        return None


def _is_baseline(source, max_side: int | None = None) -> bool:
    try:
        with Image.open(source) as img:
            return (img.format == "JPEG" and not (img.info.get("progressive") or img.info.get("progression"))
                    and (max_side is None or max(img.size) <= max_side))
    except Exception:
        return False


def is_baseline_jpeg(data: bytes) -> bool:
    """True for a JPEG that is not progressive (the only kind Rockbox can show)."""
    return _is_baseline(io.BytesIO(data))


def rockbox_sidecar(path: Path, size: int, create: bool = True) -> Path:
    """The file to copy to a Rockbox card for the sidecar art `path`: `path` itself when it is a baseline JPEG
    of at most `size` px, else its baseline copy cached in data/card-cache/covers (made when missing unless
    `create` is False; `path` when none can be made). `path` is never written. Blocking."""
    if _is_baseline(path, size):
        return path
    data = path.read_bytes()
    cached = settings.data_dir / "card-cache" / "covers" / f"{hashlib.sha256(data).hexdigest()}-{size}.jpg"
    if cached.is_file():
        return cached
    baseline = prepare_cover(data, size) if create else None
    if baseline is None:
        return path
    cached.parent.mkdir(parents=True, exist_ok=True)
    part = cached.with_name(f"{cached.name}.part")
    try:
        part.write_bytes(baseline)
        os.replace(part, cached)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return cached
