import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from config import settings
from db import init_db
from services import qobuz_client
from services.ytdlp_opts import ensure_deno_on_path
from worker import download_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
# The Qobuz token and signed CDN URLs must never reach a log line, whoever logs them.
qobuz_client.install_log_redaction()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# yt-dlp is only imported lazily inside the download engines, so doing this after
# the imports above is still early enough.
ensure_deno_on_path()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Leasure...")
    await init_db()
    await download_worker.start()
    # The bound address belongs to uvicorn (--port), which logs it itself; settings.port may not be it.
    logger.info("Leasure ready")
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
# Added last so it runs first: a DNS-rebinding page reaches 127.0.0.1 under its own host name.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

from services.platform import device_path_placeholder, get_platform  # noqa: E402

templates.env.globals["platform"] = get_platform()
templates.env.globals["device_path_placeholder"] = device_path_placeholder()

# Register routers
from routers import art, device, downloads, library, music, qobuz, spotify, youtube  # noqa: E402

app.include_router(music.router, prefix="/api/music", tags=["music"])
app.include_router(spotify.router, prefix="/api/spotify", tags=["spotify"])
app.include_router(youtube.router, prefix="/api/youtube", tags=["youtube"])
app.include_router(qobuz.router, prefix="/api/qobuz", tags=["qobuz"])
app.include_router(downloads.router, prefix="/api/downloads", tags=["downloads"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(device.router, prefix="/api/device", tags=["device"])
app.include_router(art.router, prefix="/api/art", tags=["art"])


# API endpoints used by the SPA
from fastapi import Depends, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db import get_session  # noqa: E402
from models import Track  # noqa: E402
from routers.library import sidecar_art_url  # noqa: E402


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


CAROUSEL_SIZE = 30


def _carousel_albums(rows) -> list[tuple[str, str | None, str | None]]:
    """(artist, album, cover URL or None) for the newest albums, those with art first. Blocking: stats sidecars."""
    groups: dict[tuple[str, str | None], dict] = {}
    for track_id, url, album, artist, file_path in rows:
        group = groups.setdefault((artist, album), {"url": None, "tracks": []})
        if not group["url"] and url and str(url).startswith(("https://", "http://")):
            group["url"] = url
        group["tracks"].append((track_id, file_path))
    with_art, without_art = [], []
    for (artist, album), group in groups.items():
        if len(with_art) >= CAROUSEL_SIZE:
            break
        url = group["url"] or sidecar_art_url(group["tracks"])
        (with_art if url else without_art).append((artist, album, url))
    return (with_art + without_art)[:CAROUSEL_SIZE]


@app.get("/api/home/carousel")
async def home_carousel(session: AsyncSession = Depends(get_session)):
    """LP carousel of album artwork from downloaded tracks."""
    result = await session.execute(
        select(Track.id, Track.artwork_url, Track.album, Track.artist, Track.file_path)
        .where(Track.status == "done")
        .order_by(func.coalesce(Track.downloaded_at, Track.created_at).desc(), Track.id.desc())
    )
    covers = []
    for artist, album, url in await asyncio.to_thread(_carousel_albums, result.all()):
        title = f"{escape(artist)} - {escape(album or 'Unknown album')}"
        # An album with no usable artwork still belongs in the collection: a placeholder
        # tile keeps the count honest instead of claiming there is no music.
        if url:
            covers.append(f'<img src="{escape(url)}" alt="{escape(album)}" title="{title}" data-vibrant>')
        else:
            covers.append(f'<span class="lp-cover-placeholder" title="{title}">{escape(album or "Unknown album")}</span>')
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
