"""Unified music router — provider-agnostic browsing, search, and smart download."""

import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Track
from services import spotify_client as sp
from services import youtube_client as yt
from services.music_aggregator import (
    enrich_with_download_status,
    get_unified_albums,
    get_unified_playlists,
    get_unified_recent,
    get_unique_artists,
)
from services.search import search_all
from worker import download_worker

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --- Browse endpoints (return HTML partials) ---


@router.get("/recent")
async def recent(request: Request, session: AsyncSession = Depends(get_session)):
    """Recently listened tracks from all sources."""
    tracks = get_unified_recent(limit=50)
    await enrich_with_download_status(tracks, session)
    return templates.TemplateResponse(
        request=request,
        name="partials/music_tracks.html",
        context={"tracks": tracks, "show_art": True, "show_album": True, "show_refresh": True},
    )


@router.get("/albums")
async def albums(request: Request, session: AsyncSession = Depends(get_session)):
    """All albums from all sources, merged."""
    album_list = get_unified_albums()

    # Enrich with download counts
    for album in album_list:
        stmt = select(Track).where(Track.album == album["name"], Track.status == "done")
        result = await session.execute(stmt)
        album["downloaded_count"] = len(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="partials/music_albums.html",
        context={"albums": album_list},
    )


@router.get("/playlists")
async def playlists(request: Request):
    """All playlists from all sources."""
    playlist_list = get_unified_playlists()
    return templates.TemplateResponse(
        request=request,
        name="partials/music_playlists.html",
        context={"playlists": playlist_list},
    )


@router.get("/artists")
async def artists(request: Request):
    """Unique artists from all sources."""
    artist_list = get_unique_artists()
    return templates.TemplateResponse(
        request=request,
        name="partials/music_artists.html",
        context={"artists": artist_list},
    )


@router.get("/album/{provider}/{album_id}")
async def album_detail(
    request: Request,
    provider: str,
    album_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Album detail with tracks from a specific provider."""
    album = None
    tracks = []

    if provider == "spotify":
        data = sp.get_album_tracks(album_id)
        if data:
            album = data["album"]
            album["image_url"] = album.get("image_url")
            for t in data["tracks"]:
                stmt = select(Track).where(Track.spotify_uri == t["uri"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()
                tracks.append({
                    **t,
                    "sources": [{"provider": "spotify", "id": t["id"], "uri": t["uri"],
                                 "artist_id": t.get("artist_id", "")}],
                    "download_status": existing.status if existing else None,
                })

    elif provider == "youtube":
        data = yt.get_album_tracks(album_id)
        if data:
            album = data["album"]
            for t in data["tracks"]:
                dl_status = None
                if t.get("id"):
                    stmt = select(Track).where(Track.youtube_id == t["id"])
                    result = await session.execute(stmt)
                    existing = result.scalar_one_or_none()
                    dl_status = existing.status if existing else None
                tracks.append({
                    **t,
                    "sources": [{"provider": "youtube", "id": t.get("id", "")}],
                    "download_status": dl_status,
                })

    if not album:
        return HTMLResponse("<p>Album not found or source unavailable.</p>")

    return templates.TemplateResponse(
        request=request,
        name="partials/music_album_detail.html",
        context={"album": album, "tracks": tracks, "provider": provider, "album_id": album_id},
    )


@router.get("/playlist/{provider}/{playlist_id}")
async def playlist_detail(
    request: Request,
    provider: str,
    playlist_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Playlist tracks from a specific provider."""
    playlist_info = None
    tracks = []

    if provider == "spotify":
        data = sp.get_playlist_tracks(playlist_id)
        if data and not data.get("error"):
            playlist_info = data.get("playlist", {})
            for t in data["tracks"]:
                stmt = select(Track).where(Track.spotify_uri == t["uri"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()
                tracks.append({
                    **t,
                    "sources": [{"provider": "spotify", "id": t["id"], "uri": t["uri"]}],
                    "download_status": existing.status if existing else None,
                })

    elif provider == "youtube":
        data = yt.get_playlist_tracks(playlist_id)
        if data:
            playlist_info = data.get("playlist", {})
            for t in data["tracks"]:
                dl_status = None
                if t.get("id"):
                    stmt = select(Track).where(Track.youtube_id == t["id"])
                    result = await session.execute(stmt)
                    existing = result.scalar_one_or_none()
                    dl_status = existing.status if existing else None
                tracks.append({
                    **t,
                    "sources": [{"provider": "youtube", "id": t.get("id", "")}],
                    "download_status": dl_status,
                })

    if not playlist_info:
        return HTMLResponse("<p>Playlist not found or source unavailable.</p>")

    return templates.TemplateResponse(
        request=request,
        name="partials/music_album_detail.html",
        context={"album": playlist_info, "tracks": tracks, "provider": provider,
                 "album_id": playlist_id, "is_playlist": True},
    )


# --- Search ---


@router.get("/search")
async def search(
    request: Request,
    q: str = Query("", min_length=1),
    session: AsyncSession = Depends(get_session),
):
    """Cross-source search across Spotify, YouTube Music, and MusicBrainz."""
    if not q or len(q.strip()) < 2:
        return HTMLResponse('<p class="text-muted">Type to search...</p>')

    results = await search_all(q.strip(), limit=20)

    # Enrich tracks with download status
    for track in results.get("tracks", []):
        await enrich_with_download_status([track], session)

    return templates.TemplateResponse(
        request=request,
        name="partials/search_results.html",
        context={"results": results, "query": q},
    )


# --- Smart download ---


@router.post("/download/track")
async def download_track(
    request: Request,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    track_number: int = Form(0),
    duration_ms: int = Form(0),
    image_url: str = Form(""),
    spotify_uri: str = Form(""),
    spotify_artist_id: str = Form(""),
    youtube_id: str = Form(""),
    format: str = Form("mp3"),
    session: AsyncSession = Depends(get_session),
):
    """Smart download — auto-picks best source based on format."""
    # Check if already downloaded/queued
    existing = None
    if spotify_uri:
        stmt = select(Track).where(Track.spotify_uri == spotify_uri)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
    if not existing and youtube_id:
        stmt = select(Track).where(Track.youtube_id == youtube_id)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

    if existing and existing.status == "done":
        if _is_htmx(request):
            return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                              context={"status": "already_downloaded"})
        return {"status": "already_downloaded", "track_id": existing.id}
    if existing and existing.status in ("pending", "downloading"):
        if _is_htmx(request):
            return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                              context={"status": "already_queued"})
        return {"status": "already_queued", "track_id": existing.id}

    # Determine quality
    quality = "mp3_320"
    if format == "flac":
        quality = "flac_lossy"
    elif format == "flac_lossless":
        quality = "flac_lossless"

    # Auto-pick source: prefer spotify (searches YouTube Music anyway), fallback to youtube
    source = "spotify" if spotify_uri else "youtube"

    # Fetch genre from Spotify artist if available
    genre = None
    if spotify_artist_id:
        genres = sp.get_artist_genres(spotify_artist_id)
        if genres:
            genre = ", ".join(genres[:3])

    track = existing or Track()
    if spotify_uri:
        track.spotify_uri = spotify_uri
    if youtube_id:
        track.youtube_id = youtube_id
    track.title = title
    track.artist = artist
    track.album = album
    track.genre = genre
    track.track_number = track_number
    track.duration_ms = duration_ms
    track.artwork_url = image_url
    track.format = "flac" if "flac" in format else "mp3"
    track.quality = quality
    track.source = source
    track.status = "pending"
    track.error_message = None

    if not existing:
        session.add(track)
    await session.commit()
    await session.refresh(track)

    await download_worker.enqueue(track.id)

    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                          context={"status": "queued"})
    return {"status": "queued", "track_id": track.id}


@router.post("/download/album/{provider}/{album_id}")
async def download_album(
    request: Request,
    provider: str,
    album_id: str,
    format: str = Form("mp3"),
    session: AsyncSession = Depends(get_session),
):
    """Download all tracks from an album using the specified provider."""
    album_data = None
    tracks_data = []

    if provider == "spotify":
        data = sp.get_album_tracks(album_id)
        if data:
            album_data = data["album"]
            tracks_data = data["tracks"]
    elif provider == "youtube":
        data = yt.get_album_tracks(album_id)
        if data:
            album_data = data["album"]
            tracks_data = data["tracks"]

    if not album_data:
        return HTMLResponse("<p>Album not found.</p>") if _is_htmx(request) else {"error": "Album not found"}

    quality = "mp3_320"
    if format == "flac":
        quality = "flac_lossy"
    elif format == "flac_lossless":
        quality = "flac_lossless"

    # Fetch genre
    album_genre = None
    if provider == "spotify":
        album_genres = album_data.get("genres", [])
        if not album_genres and tracks_data:
            artist_genres = sp.get_artist_genres(tracks_data[0].get("artist_id", ""))
            if artist_genres:
                album_genre = ", ".join(artist_genres[:3])
        elif album_genres:
            album_genre = ", ".join(album_genres[:3])

    release_date = album_data.get("release_date", "")
    year = int(release_date[:4]) if release_date and len(release_date) >= 4 else None
    if not year and album_data.get("year"):
        try:
            year = int(album_data["year"])
        except (ValueError, TypeError):
            pass

    queued = []
    for t in tracks_data:
        # Find existing by source
        existing = None
        if provider == "spotify" and t.get("uri"):
            stmt = select(Track).where(Track.spotify_uri == t["uri"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
        elif provider == "youtube" and t.get("id"):
            stmt = select(Track).where(Track.youtube_id == t["id"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

        if existing and existing.status in ("done", "pending", "downloading"):
            queued.append({"track_id": existing.id, "status": existing.status})
            continue

        track = existing or Track()
        if provider == "spotify":
            track.spotify_uri = t.get("uri")
            track.source = "spotify"
        elif provider == "youtube":
            track.youtube_id = t.get("id")
            track.source = "youtube"

        track.title = t["name"]
        track.artist = t.get("artist", "")
        track.album = album_data["name"]
        track.album_artist = album_data.get("artist", "")
        track.track_number = t.get("track_number") or t.get("index")
        track.disc_number = t.get("disc_number")
        track.duration_ms = t.get("duration_ms", 0)
        track.artwork_url = album_data.get("image_url", "")
        track.year = year
        track.genre = album_genre
        track.format = "flac" if "flac" in format else "mp3"
        track.quality = quality
        track.status = "pending"
        track.error_message = None

        if not existing:
            session.add(track)
        await session.commit()
        await session.refresh(track)

        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    if _is_htmx(request):
        # Re-render album detail with updated statuses
        tracks_with_status = []
        for t in tracks_data:
            dl_status = None
            if provider == "spotify" and t.get("uri"):
                stmt = select(Track).where(Track.spotify_uri == t["uri"])
                result = await session.execute(stmt)
                ex = result.scalar_one_or_none()
                dl_status = ex.status if ex else None
            elif provider == "youtube" and t.get("id"):
                stmt = select(Track).where(Track.youtube_id == t["id"])
                result = await session.execute(stmt)
                ex = result.scalar_one_or_none()
                dl_status = ex.status if ex else None
            tracks_with_status.append({
                **t,
                "sources": [{"provider": provider, "id": t.get("id", ""), "uri": t.get("uri", "")}],
                "download_status": dl_status,
            })
        return templates.TemplateResponse(
            request=request,
            name="partials/music_album_detail.html",
            context={"album": album_data, "tracks": tracks_with_status, "provider": provider, "album_id": album_id},
        )

    return {"album": album_data.get("name", ""), "tracks_queued": len(queued), "details": queued}


@router.post("/download/playlist/{provider}/{playlist_id}")
async def download_playlist(
    request: Request,
    provider: str,
    playlist_id: str,
    format: str = Form("mp3"),
    session: AsyncSession = Depends(get_session),
):
    """Download all tracks from a playlist using the specified provider."""
    playlist_data = None
    tracks_data = []

    if provider == "spotify":
        data = sp.get_playlist_tracks(playlist_id)
        if data and not data.get("error"):
            playlist_data = data.get("playlist", {})
            tracks_data = data["tracks"]
    elif provider == "youtube":
        data = yt.get_playlist_tracks(playlist_id)
        if data:
            playlist_data = data.get("playlist", {})
            tracks_data = data.get("tracks", [])

    if not playlist_data:
        return HTMLResponse("<p>Playlist not found.</p>") if _is_htmx(request) else {"error": "Playlist not found"}

    quality = "mp3_320"
    if format == "flac":
        quality = "flac_lossy"
    elif format == "flac_lossless":
        quality = "flac_lossless"

    queued = []
    for t in tracks_data:
        existing = None
        if provider == "spotify" and t.get("uri"):
            stmt = select(Track).where(Track.spotify_uri == t["uri"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
        elif provider == "youtube" and t.get("id"):
            stmt = select(Track).where(Track.youtube_id == t["id"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

        if existing and existing.status in ("done", "pending", "downloading"):
            queued.append({"track_id": existing.id, "status": existing.status})
            continue

        track = existing or Track()
        if provider == "spotify":
            track.spotify_uri = t.get("uri")
            track.source = "spotify"
        elif provider == "youtube":
            track.youtube_id = t.get("id")
            track.source = "youtube"

        track.title = t["name"]
        track.artist = t.get("artist", "")
        track.album = t.get("album", playlist_data.get("name", ""))
        track.album_artist = t.get("album_artist", t.get("artist", ""))
        track.track_number = t.get("track_number") or t.get("index")
        track.disc_number = t.get("disc_number")
        track.duration_ms = t.get("duration_ms", 0)
        track.artwork_url = t.get("image_url") or playlist_data.get("image_url", "")
        track.format = "flac" if "flac" in format else "mp3"
        track.quality = quality
        track.status = "pending"
        track.error_message = None

        if not existing:
            session.add(track)
        await session.commit()
        await session.refresh(track)

        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    if _is_htmx(request):
        tracks_with_status = []
        for t in tracks_data:
            dl_status = None
            if provider == "spotify" and t.get("uri"):
                stmt = select(Track).where(Track.spotify_uri == t["uri"])
                result = await session.execute(stmt)
                ex = result.scalar_one_or_none()
                dl_status = ex.status if ex else None
            elif provider == "youtube" and t.get("id"):
                stmt = select(Track).where(Track.youtube_id == t["id"])
                result = await session.execute(stmt)
                ex = result.scalar_one_or_none()
                dl_status = ex.status if ex else None
            tracks_with_status.append({
                **t,
                "sources": [{"provider": provider, "id": t.get("id", ""), "uri": t.get("uri", "")}],
                "download_status": dl_status,
            })
        return templates.TemplateResponse(
            request=request,
            name="partials/music_album_detail.html",
            context={"album": playlist_data, "tracks": tracks_with_status, "provider": provider,
                     "album_id": playlist_id, "is_playlist": True},
        )

    return {"playlist": playlist_data.get("name", ""), "tracks_queued": len(queued), "details": queued}
