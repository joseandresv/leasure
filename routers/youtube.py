import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import get_session
from models import Playlist, PlaylistTrack, Track
from services import oauth_state
from services import youtube_client as yt
from services.formats import resolve_format
from worker import download_worker


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/status")
async def status():
    connected = await asyncio.to_thread(yt.is_connected)
    if connected:
        return {"connected": True}
    return {"connected": False, "message": "YouTube Music not connected."}


@router.get("/status/html")
async def status_html(request: Request):
    connected = await asyncio.to_thread(yt.is_connected)
    return templates.TemplateResponse(request=request, name="partials/yt_music_status.html",
                                      context={"connected": connected})


@router.post("/setup")
async def setup(headers_raw: str = Form(...)):
    success = await asyncio.to_thread(yt.setup_from_headers, headers_raw)
    if success:
        return HTMLResponse('<p><mark>Connected!</mark> Reload the page to browse your library.</p>')
    return HTMLResponse('<p style="color: var(--pico-del-color);">Failed to connect. Make sure you copied the full request headers.</p>')


@router.get("/oauth/connect")
async def oauth_connect():
    """Redirect to Google OAuth2 for YouTube history access."""
    url = yt.get_youtube_oauth_url(state=oauth_state.issue())
    if not url:
        return HTMLResponse('<p style="color: var(--pico-del-color);">Google OAuth not configured. '
                            'Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env</p>')
    return RedirectResponse(url)


@router.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Google OAuth2 callback."""
    if error:
        logger.info("YouTube authorization not granted: %s", error)
        return RedirectResponse("/?yt_oauth=denied")
    if not oauth_state.verify(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state. Start the YouTube connection again.")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
    success = await asyncio.to_thread(yt.handle_youtube_oauth_callback, code)
    if success:
        return RedirectResponse("/?yt_oauth=ok")
    return HTMLResponse('<p style="color: var(--pico-del-color);">YouTube OAuth failed. Please try again.</p>')


@router.get("/oauth/status")
async def oauth_status():
    """Check YouTube OAuth connection status."""
    connected = await asyncio.to_thread(yt.is_youtube_oauth_connected)
    configured = bool(settings.google_client_id)
    return {"connected": connected, "configured": configured}


@router.get("/oauth/status/html")
async def oauth_status_html(request: Request):
    """HTML status for YouTube OAuth (history access)."""
    connected = await asyncio.to_thread(yt.is_youtube_oauth_connected)
    return templates.TemplateResponse(request=request, name="partials/yt_oauth_status.html",
                                      context={"connected": connected,
                                               "configured": bool(settings.google_client_id),
                                               "redirect_uri": settings.google_redirect_uri})


@router.get("/playlists")
async def playlists():
    result = await asyncio.to_thread(yt.get_playlists)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return result


@router.get("/playlists/html")
async def playlists_html(request: Request):
    result = await asyncio.to_thread(yt.get_playlists)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return templates.TemplateResponse(request=request, name="partials/yt_playlist_list.html",
                                      context={"playlists": result})


@router.get("/playlists/{playlist_id}")
async def playlist_tracks(
    playlist_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await asyncio.to_thread(yt.get_playlist_tracks, playlist_id)
    if result is None:
        return {"error": "Not connected to YouTube Music"}

    for track in result["tracks"]:
        stmt = select(Track).where(Track.youtube_id == track["id"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return result


@router.get("/playlists/{playlist_id}/html")
async def playlist_tracks_html(
    request: Request,
    playlist_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await asyncio.to_thread(yt.get_playlist_tracks, playlist_id)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')

    for track in result["tracks"]:
        stmt = select(Track).where(Track.youtube_id == track["id"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    playlist_ctx = result.get("playlist", {})
    playlist_ctx["id"] = playlist_id
    return templates.TemplateResponse(request=request, name="partials/yt_playlist_tracks.html",
                                      context={"playlist": playlist_ctx, "tracks": result["tracks"]})


@router.get("/albums")
async def albums():
    result = await asyncio.to_thread(yt.get_library_albums)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return result


@router.get("/albums/html")
async def albums_html(request: Request):
    result = await asyncio.to_thread(yt.get_library_albums)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return templates.TemplateResponse(request=request, name="partials/yt_albums.html",
                                      context={"albums": result})


@router.get("/albums/{browse_id}")
async def album_tracks(
    browse_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await asyncio.to_thread(yt.get_album_tracks, browse_id)
    if result is None:
        return {"error": "Not connected to YouTube Music"}

    for track in result["tracks"]:
        if track["id"]:
            stmt = select(Track).where(Track.youtube_id == track["id"])
            res = await session.execute(stmt)
            existing = res.scalar_one_or_none()
            track["download_status"] = existing.status if existing else None

    return result


@router.get("/albums/{browse_id}/html")
async def album_tracks_html(
    request: Request,
    browse_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = await asyncio.to_thread(yt.get_album_tracks, browse_id)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')

    for track in result["tracks"]:
        if track["id"]:
            stmt = select(Track).where(Track.youtube_id == track["id"])
            res = await session.execute(stmt)
            existing = res.scalar_one_or_none()
            track["download_status"] = existing.status if existing else None

    album = result.get("album", {})
    return templates.TemplateResponse(request=request, name="partials/yt_album_tracks.html",
                                      context={"album": album, "tracks": result["tracks"]})


@router.get("/liked")
async def liked_songs(session: AsyncSession = Depends(get_session)):
    result = await asyncio.to_thread(yt.get_liked_songs)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')

    for track in result:
        stmt = select(Track).where(Track.youtube_id == track["id"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return result


@router.get("/liked/html")
async def liked_songs_html(request: Request, session: AsyncSession = Depends(get_session)):
    result = await asyncio.to_thread(yt.get_liked_songs)
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')

    for track in result:
        stmt = select(Track).where(Track.youtube_id == track["id"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return templates.TemplateResponse(request=request, name="partials/yt_tracks.html",
                                      context={"tracks": result})


@router.post("/download/track")
async def download_track(
    request: Request,
    video_id: str,
    title: str,
    artist: str,
    album: str = "",
    track_number: int = 0,
    duration_ms: int = 0,
    image_url: str = "",
    format: str = "mp3",
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Track).where(Track.youtube_id == video_id)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing and existing.status == "done":
        status = "already_downloaded"
        if _is_htmx(request):
            return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                              context={"status": status})
        return {"status": status, "track_id": existing.id}
    if existing and existing.status in ("pending", "downloading"):
        status = "already_queued"
        if _is_htmx(request):
            return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                              context={"status": status})
        return {"status": status, "track_id": existing.id}

    quality, container = resolve_format(format)

    track = existing or Track(youtube_id=video_id)
    track.title = title
    track.artist = artist
    track.album = album
    track.track_number = track_number
    track.duration_ms = duration_ms
    track.artwork_url = image_url
    track.format = container
    track.quality = quality
    track.source = "youtube"
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


@router.post("/download/playlist/{playlist_id}")
async def download_playlist(
    request: Request,
    playlist_id: str,
    format: str = "mp3",
    session: AsyncSession = Depends(get_session),
):
    playlist_data = yt.get_playlist_tracks(playlist_id)
    if playlist_data is None:
        return {"error": "Not connected to YouTube Music"}

    pl_info = playlist_data.get("playlist", {})
    pl_name = pl_info.get("name", "Untitled Playlist")
    tracks_data = playlist_data["tracks"]

    if not tracks_data:
        return {"error": "No tracks in playlist"}

    # Create or update playlist in DB
    stmt = select(Playlist).where(Playlist.source == "youtube", Playlist.source_id == playlist_id)
    result = await session.execute(stmt)
    db_playlist = result.scalar_one_or_none()

    if not db_playlist:
        db_playlist = Playlist(name=pl_name, source="youtube", source_id=playlist_id,
                               image_url=pl_info.get("image_url"))
        session.add(db_playlist)
        await session.commit()
        await session.refresh(db_playlist)
    else:
        db_playlist.name = pl_name
        from sqlalchemy import delete
        await session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == db_playlist.id))
        await session.commit()

    queued = []
    for position, t in enumerate(tracks_data):
        video_id = t["id"]
        if not video_id:
            continue

        stmt = select(Track).where(Track.youtube_id == video_id)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing and existing.status in ("done", "pending", "downloading"):
            session.add(PlaylistTrack(playlist_id=db_playlist.id, track_id=existing.id, position=position))
            queued.append({"track_id": existing.id, "status": existing.status})
            continue

        quality, container = resolve_format(format)

        track = existing or Track(youtube_id=video_id)
        track.title = t["name"]
        track.artist = t.get("artist", "")
        track.album = t.get("album", "")
        track.duration_ms = t.get("duration_ms", 0)
        track.artwork_url = t.get("image_url", "")
        track.format = container
        track.quality = quality
        track.source = "youtube"
        track.status = "pending"
        track.error_message = None

        if not existing:
            session.add(track)
        await session.commit()
        await session.refresh(track)

        session.add(PlaylistTrack(playlist_id=db_playlist.id, track_id=track.id, position=position))
        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    await session.commit()
    resp = {"playlist": pl_name, "tracks_queued": len(queued), "details": queued}
    if _is_htmx(request):
        # Re-render playlist tracks with updated statuses
        for t in tracks_data:
            if t["id"]:
                stmt = select(Track).where(Track.youtube_id == t["id"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()
                t["download_status"] = existing.status if existing else None
        playlist_ctx = pl_info.copy()
        playlist_ctx["id"] = playlist_id
        return templates.TemplateResponse(request=request, name="partials/yt_playlist_tracks.html",
                                          context={"playlist": playlist_ctx, "tracks": tracks_data})
    return resp
