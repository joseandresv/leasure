import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config import settings
from db import init_db
from worker import download_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Ensure deno is on PATH for yt-dlp Premium quality downloads
# (deno installs to ~/.deno/bin on all platforms; os.pathsep keeps this Windows-safe).
# yt-dlp is only imported lazily inside the download engines, so doing this after
# the imports above is still early enough.
_deno_bin = Path.home() / ".deno" / "bin"
if _deno_bin.exists() and str(_deno_bin) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_deno_bin}{os.pathsep}{os.environ.get('PATH', '')}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Leasure...")
    await init_db()
    await download_worker.start()
    logger.info("Leasure ready at http://%s:%d", settings.host, settings.port)
    yield
    await download_worker.stop()
    logger.info("Leasure shutdown complete")


app = FastAPI(title="Leasure", version="0.2.0", lifespan=lifespan)


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests that a browser marks as cross-site.

    Every mutating endpoint is called from our own page (htmx / fetch), so a
    request whose Sec-Fetch-Site says "cross-site" or whose Origin does not
    match the Host is a drive-by page on another tab, not the user. GET stays
    open (nothing mutating is a GET any more)."""

    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            site = request.headers.get("sec-fetch-site")
            if site in ("cross-site", "same-site"):
                return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if origin and host:
                origin_host = origin.split("://", 1)[-1]
                if origin_host != host:
                    return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        return await call_next(request)


app.add_middleware(SameOriginMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

from services.platform import device_path_placeholder, get_platform  # noqa: E402

templates.env.globals["platform"] = get_platform()
templates.env.globals["device_path_placeholder"] = device_path_placeholder()

# Register routers
from routers import device, downloads, library, music, spotify, youtube  # noqa: E402

app.include_router(music.router, prefix="/api/music", tags=["music"])
app.include_router(spotify.router, prefix="/api/spotify", tags=["spotify"])
app.include_router(youtube.router, prefix="/api/youtube", tags=["youtube"])
app.include_router(downloads.router, prefix="/api/downloads", tags=["downloads"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(device.router, prefix="/api/device", tags=["device"])


# API endpoints used by the SPA
from fastapi import Depends, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db import get_session  # noqa: E402
from models import Track  # noqa: E402


@app.get("/api/status/bar")
async def status_bar(session: AsyncSession = Depends(get_session)):
    total = await session.scalar(select(func.count(Track.id)).where(Track.status == "done")) or 0
    total_size = await session.scalar(select(func.sum(Track.file_size)).where(Track.status == "done")) or 0
    pending = await session.scalar(select(func.count(Track.id)).where(Track.status.in_(["pending", "downloading"]))) or 0
    errors = await session.scalar(select(func.count(Track.id)).where(Track.status == "error")) or 0

    size_mb = round(total_size / (1024 * 1024), 1)
    parts = [f"library: {total} tracks ({size_mb}mb)"]
    if pending:
        parts.append(f'<span class="active">downloading: {pending}</span>')
    if errors:
        parts.append(f"errors: {errors}")

    queue_html = " | ".join(parts)

    # Get recent track being downloaded
    downloading = await session.execute(
        select(Track).where(Track.status == "downloading").limit(1)
    )
    dl_track = downloading.scalar_one_or_none()
    now_html = ""
    if dl_track:
        now_html = (
            f' | <span class="audio-bars"><span></span><span></span><span></span><span></span></span> '
            f'{escape(dl_track.artist)} - {escape(dl_track.title)}'
        )

    return HTMLResponse(f'<span>{queue_html}{now_html}</span>')


@app.get("/api/home/carousel")
async def home_carousel(session: AsyncSession = Depends(get_session)):
    """LP carousel of album artwork from downloaded tracks."""
    result = await session.execute(
        select(Track.artwork_url, Track.album, Track.artist)
        .where(Track.status == "done", Track.artwork_url.isnot(None))
        .group_by(Track.artist, Track.album)
        .limit(30)
    )
    covers = []
    for url, album, artist in result.all():
        if not url or not str(url).startswith(("https://", "http://")):
            continue
        covers.append(f'<img src="{escape(url)}" alt="{escape(album)}" title="{escape(artist)} - {escape(album)}" data-vibrant>')
    if not covers:
        return HTMLResponse('<i style="color:var(--text-muted)">no music yet &mdash; download some tracks to see your collection</i>')

    # The marquee shifts the strip by -50%, so duplicating only loops seamlessly when one
    # copy already fills the panel (80px covers + 0.5rem gap => 9 covers cover ~700px).
    # Fewer than that: render each cover once and stop the animation, or it scrolls off.
    strip = "".join(covers)
    if len(covers) > 8:
        inner = f'<div class="lp-carousel-inner">{strip}{strip}</div>'
    else:
        inner = f'<div class="lp-carousel-inner" style="animation:none">{strip}</div>'
    html = f'<div class="lp-carousel">{inner}</div>'

    total = await session.scalar(select(func.count(Track.id)).where(Track.status == "done")) or 0
    album_groups = (
        select(Track.artist, Track.album).where(Track.status == "done").group_by(Track.artist, Track.album).subquery()
    )
    albums_count = await session.scalar(select(func.count()).select_from(album_groups)) or 0
    html += (
        f'<p style="font-size:0.85rem;color:var(--text-muted)">'
        f'{total} track{"s" if total != 1 else ""} across '
        f'{albums_count} album{"s" if albums_count != 1 else ""}</p>'
    )
    return HTMLResponse(html)


# Single page route — the SPA shell
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# Redirect old page routes to root (SPA handles navigation)
from fastapi.responses import RedirectResponse  # noqa: E402


@app.get("/spotify")
@app.get("/youtube")
@app.get("/downloads")
@app.get("/library")
@app.get("/device")
async def redirect_to_spa():
    return RedirectResponse("/")


def main():
    uvicorn.run("app:app", host=settings.host, port=settings.port, reload=True)


if __name__ == "__main__":
    main()
