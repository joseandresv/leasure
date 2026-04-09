from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Track
from worker import download_worker

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/queue")
async def queue_status(session: AsyncSession = Depends(get_session)):
    stmt = select(Track).where(Track.status.in_(["pending", "downloading", "converting", "tagging"]))
    result = await session.execute(stmt)
    tracks = result.scalars().all()
    return {
        "queue_size": download_worker.queue_size,
        "tracks": [
            {
                "id": t.id,
                "title": t.title,
                "artist": t.artist,
                "status": t.status,
                "format": t.format,
                "quality": t.quality,
                "error_message": t.error_message,
            }
            for t in tracks
        ],
    }


@router.get("/queue/html")
async def queue_status_html(request: Request, session: AsyncSession = Depends(get_session)):
    stmt = select(Track).where(Track.status.in_(["pending", "downloading", "converting", "tagging"]))
    result = await session.execute(stmt)
    tracks = result.scalars().all()
    return templates.TemplateResponse(request=request, name="partials/queue_status.html",
                                      context={"tracks": [
                                          {"id": t.id, "title": t.title, "artist": t.artist,
                                           "status": t.status, "format": t.format}
                                          for t in tracks
                                      ]})


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
                                           "album": t.album, "format": t.format, "quality": t.quality,
                                           "engine_used": t.engine_used,
                                           "downloaded_at": t.downloaded_at.isoformat() if t.downloaded_at else None}
                                          for t in tracks
                                      ]})
