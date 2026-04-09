from collections import defaultdict

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


@router.get("/graph")
async def genre_graph(session: AsyncSession = Depends(get_session)):
    """Return genre-based graph data for Sigma.js visualization."""
    result = await session.execute(
        select(
            Track.album,
            func.min(Track.album_artist).label("artist"),
            func.min(Track.artwork_url).label("artwork_url"),
            func.min(Track.genre).label("genre"),
        )
        .where(Track.status == "done", Track.album.isnot(None))
        .group_by(Track.album)
    )
    albums = result.all()

    nodes = []
    genre_index = defaultdict(list)

    for i, (album, artist, artwork, genre_str) in enumerate(albums):
        node_id = f"album_{i}"
        genres = [g.strip().lower() for g in genre_str.split(",")] if genre_str else []
        nodes.append({
            "id": node_id,
            "label": album or "Unknown",
            "artist": artist or "Unknown",
            "image": artwork or "",
            "genres": genres,
        })
        for genre in genres:
            if genre:
                genre_index[genre].append(node_id)

    # Edges: connect albums sharing genres
    edges = []
    seen_edges = set()
    for genre, album_ids in genre_index.items():
        for a in album_ids:
            for b in album_ids:
                if a < b:
                    edge_key = f"{a}-{b}"
                    if edge_key not in seen_edges:
                        edges.append({"source": a, "target": b, "genre": genre})
                        seen_edges.add(edge_key)

    # Assign colors to genres
    palette = ["#0066ff", "#c9a961", "#00cc66", "#cc3333", "#ff9900",
               "#9966ff", "#ff6699", "#00cccc", "#ff6600", "#6699ff"]
    genre_colors = {}
    for i, genre in enumerate(sorted(genre_index.keys())):
        genre_colors[genre] = {
            "color": palette[i % len(palette)],
            "count": len(genre_index[genre]),
        }

    return {"nodes": nodes, "edges": edges, "genres": genre_colors}
