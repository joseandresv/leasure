import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Track
from routers import device as device_router
from services import qobuz_session
from services.downloader import QOBUZ_ROUTED
from worker import download_worker

router = APIRouter()
templates = Jinja2Templates(directory="templates")

ACTIVE_STATUSES = ["downloading", "converting", "tagging", "pending"]
# Failures stay in the queue until the user retries or dismisses them: an error that is
# only visible as a counter in the footer is an error nobody fixes.
QUEUE_STATUSES = [*ACTIVE_STATUSES, "error"]
UPGRADE_QUEUE_STATES = ("queued", "downloading", "error")
_UPGRADE_STATUS = {"queued": "pending", "downloading": "downloading", "error": "error"}
_QUEUE_ORDER = {"downloading": 0, "converting": 0, "tagging": 0, "pending": 1, "error": 2}


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _format_downloaded_at(value: datetime | None) -> str | None:
    """Display form of a naive UTC timestamp, shown in the server's local zone."""
    if not value:
        return None
    return value.replace(tzinfo=UTC).astimezone().strftime("%d %b %Y, %H:%M")


def _route(track: Track, qobuz_ready: bool) -> str:
    if track.quality not in QOBUZ_ROUTED or not qobuz_ready:
        return "youtube"
    return "qobuz_only" if track.quality == "flac_lossless" else "qobuz_first"


def _queue_row(track: Track, qobuz_ready: bool) -> dict:
    """A download, or a finished track's FLAC upgrade, as the queue shows it."""
    upgrade = track.status == "done" and track.upgrade_state in UPGRADE_QUEUE_STATES
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "format": track.format,
        "quality": track.quality,
        "kind": "upgrade" if upgrade else "download",
        "status": _UPGRADE_STATUS[track.upgrade_state] if upgrade else track.status,
        "error_message": track.upgrade_error if upgrade else track.error_message,
        "route": "upgrade" if upgrade else _route(track, qobuz_ready),
    }


async def _qobuz_ready() -> bool:
    return await asyncio.to_thread(qobuz_session.usable) is not None


async def _queue_rows(session: AsyncSession) -> list[dict]:
    """Everything the user still has to care about: downloading, then pending, then failed."""
    stmt = select(Track).where(or_(Track.status.in_(QUEUE_STATUSES),
                                   and_(Track.status == "done", Track.upgrade_state.in_(UPGRADE_QUEUE_STATES))))
    result = await session.execute(stmt)
    ready = await _qobuz_ready()
    rows = [_queue_row(t, ready) for t in result.scalars().all()]
    return sorted(rows, key=lambda r: (_QUEUE_ORDER.get(r["status"], 3), r["id"]))


def _sync_running() -> bool:
    return any(not job["task"].done() for job in device_router._SYNC_JOBS.values())


@router.get("/queue")
async def queue_status(session: AsyncSession = Depends(get_session)):
    """Queue contents plus `active`: work in flight, which the desktop launcher waits on
    before it stops the server."""
    rows = await _queue_rows(session)
    active = any(r["status"] in ACTIVE_STATUSES for r in rows) or _sync_running()
    return {"queue_size": download_worker.queue_size, "active": active, "tracks": rows}


@router.get("/queue/html")
async def queue_status_html(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(request=request, name="partials/queue_status.html",
                                      context={"tracks": await _queue_rows(session)})


@router.post("/retry/{track_id}")
async def retry_download(request: Request, track_id: int, session: AsyncSession = Depends(get_session)):
    """Clear a failed track's error and put it back on the download queue."""
    track = await session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="That download is no longer in the queue.")
    if track.status == "done":
        raise HTTPException(status_code=409, detail="This track is already in the library.")
    track.status = "pending"
    track.error_message = None
    await session.commit()
    await download_worker.enqueue(track.id)
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/queue_item.html",
                                          context={"t": _queue_row(track, await _qobuz_ready())})
    return {"status": "pending", "track_id": track.id}


@router.post("/dismiss/{track_id}")
async def dismiss_download(request: Request, track_id: int, session: AsyncSession = Depends(get_session)):
    """Drop a failed track from the queue. Anything with a file belongs to the library."""
    track = await session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="That download is no longer in the queue.")
    if track.file_path:
        raise HTTPException(status_code=400,
                            detail="This track has a file in the library; remove it from the library instead.")
    await session.delete(track)
    await session.commit()
    if _is_htmx(request):
        return HTMLResponse("")
    return {"status": "dismissed", "track_id": track_id}


@router.get("/history")
async def download_history(
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Track)
        .where(Track.status == "done")
        .order_by(Track.downloaded_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    tracks = result.scalars().all()
    return [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist,
            "album": t.album,
            "format": t.format,
            "quality": t.quality,
            "engine_used": t.engine_used,
            "downloaded_at": t.downloaded_at.isoformat() if t.downloaded_at else None,
        }
        for t in tracks
    ]


@router.get("/history/html")
async def download_history_html(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Track)
        .where(Track.status == "done")
        .order_by(Track.downloaded_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    tracks = result.scalars().all()
    return templates.TemplateResponse(request=request, name="partials/download_history.html",
                                      context={"tracks": [
                                          {"id": t.id, "title": t.title, "artist": t.artist,
                                           "album": t.album, "engine_used": t.engine_used,
                                           "downloaded_at": _format_downloaded_at(t.downloaded_at),
                                           **t.quality_readout()}
                                          for t in tracks
                                      ]})
