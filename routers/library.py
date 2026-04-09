from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Track

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/stats")
async def library_stats(session: AsyncSession = Depends(get_session)):
    total = await session.scalar(select(func.count(Track.id)).where(Track.status == "done"))
    total_size = await session.scalar(select(func.sum(Track.file_size)).where(Track.status == "done"))
    return {
        "total_tracks": total or 0,
        "total_size_mb": round((total_size or 0) / (1024 * 1024), 1),
    }


@router.get("/stats/html")
async def library_stats_html(request: Request, session: AsyncSession = Depends(get_session)):
    total = await session.scalar(select(func.count(Track.id)).where(Track.status == "done"))
    total_size = await session.scalar(select(func.sum(Track.file_size)).where(Track.status == "done"))
    return templates.TemplateResponse(request=request, name="partials/dashboard_stats.html",
                                      context={"total_tracks": total or 0, "total_size_mb": round((total_size or 0) / (1024 * 1024), 1)})


async def _query_tracks(q: str, limit: int, offset: int, session: AsyncSession):
    stmt = select(Track).where(Track.status == "done")
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            (Track.title.ilike(pattern))
            | (Track.artist.ilike(pattern))
            | (Track.album.ilike(pattern))
        )
    stmt = stmt.order_by(Track.artist, Track.album, Track.track_number).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.get("/tracks")
async def list_tracks(
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    tracks = await _query_tracks(q, limit, offset, session)
    return [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist,
            "album": t.album,
            "track_number": t.track_number,
            "format": t.format,
            "quality": t.quality,
            "file_path": t.file_path,
            "synced_at": t.synced_at.isoformat() if t.synced_at else None,
        }
        for t in tracks
    ]


@router.get("/tracks/html")
async def list_tracks_html(
    request: Request,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    tracks = await _query_tracks(q, limit, offset, session)
    return templates.TemplateResponse(request=request, name="partials/library_tracks.html",
                                      context={"tracks": [
                                          {"id": t.id, "title": t.title, "artist": t.artist,
                                           "album": t.album, "track_number": t.track_number,
                                           "format": t.format, "quality": t.quality,
                                           "synced_at": t.synced_at}
                                          for t in tracks
                                      ]})
