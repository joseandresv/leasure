import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure deno is on PATH for yt-dlp Premium quality downloads
_deno_bin = Path.home() / ".deno" / "bin"
if _deno_bin.exists() and str(_deno_bin) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_deno_bin}:{os.environ.get('PATH', '')}"

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import settings
from db import init_db
from worker import download_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Leasure...")
    await init_db()
    await download_worker.start()
    logger.info("Leasure ready at http://%s:%d", settings.host, settings.port)
    yield
    await download_worker.stop()
    logger.info("Leasure shutdown complete")


app = FastAPI(title="Leasure", version="0.1.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Register routers
from routers import device, downloads, library, spotify, youtube  # noqa: E402

app.include_router(spotify.router, prefix="/api/spotify", tags=["spotify"])
app.include_router(youtube.router, prefix="/api/youtube", tags=["youtube"])
app.include_router(downloads.router, prefix="/api/downloads", tags=["downloads"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(device.router, prefix="/api/device", tags=["device"])


# Page routes
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
    if dl_track:
        queue_html += f' | now: {dl_track.artist} - {dl_track.title}'

    return HTMLResponse(f'<span>{queue_html}</span>')


@app.get("/api/home/carousel")
async def home_carousel(session: AsyncSession = Depends(get_session)):
    """LP carousel of album artwork from downloaded tracks."""
    result = await session.execute(
        select(Track.artwork_url, Track.album, Track.artist)
        .where(Track.status == "done", Track.artwork_url.isnot(None))
        .group_by(Track.album)
        .limit(30)
    )
    albums = result.all()
    if not albums:
        return HTMLResponse("<i>no music yet &mdash; download some tracks to see your collection spinning</i>")

    imgs = ""
    for url, album, artist in albums:
        if url:
            imgs += f'<img src="{url}" alt="{album}" title="{artist} - {album}">'
    # Duplicate for seamless loop
    html = f'<div class="lp-carousel"><div class="lp-carousel-inner">{imgs}{imgs}</div></div>'

    total = await session.scalar(select(func.count(Track.id)).where(Track.status == "done")) or 0
    albums_count = len(albums)
    html += f'<p style="font-size:0.85rem;color:var(--text-muted)">{total} tracks across {albums_count} albums</p>'
    return HTMLResponse(html)


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/spotify")
async def spotify_page(request: Request):
    return templates.TemplateResponse(request=request, name="spotify.html")


@app.get("/youtube")
async def youtube_page(request: Request):
    return templates.TemplateResponse(request=request, name="youtube.html")


@app.get("/downloads")
async def downloads_page(request: Request):
    return templates.TemplateResponse(request=request, name="downloads.html")


@app.get("/library")
async def library_page(request: Request):
    return templates.TemplateResponse(request=request, name="library.html")


@app.get("/device")
async def device_page(request: Request):
    return templates.TemplateResponse(request=request, name="device.html")


def main():
    uvicorn.run("app:app", host=settings.host, port=settings.port, reload=True)


if __name__ == "__main__":
    main()
