from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Playlist, PlaylistTrack, Track
from services import youtube_client as yt
from worker import download_worker

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/status")
async def status():
    connected = yt.is_connected()
    if connected:
        return {"connected": True}
    return {"connected": False, "message": "YouTube Music not connected."}


@router.get("/status/html")
async def status_html(request: Request):
    connected = yt.is_connected()
    if connected:
        return HTMLResponse('<p><mark>Connected</mark> to YouTube Music</p>')
    return HTMLResponse('''
        <details open>
            <summary>Not connected to YouTube Music. Click to set up.</summary>
            <p>To connect YouTube Music:</p>
            <ol>
                <li>Open <a href="https://music.youtube.com" target="_blank">music.youtube.com</a> in Chrome while logged in</li>
                <li>Press <strong>F12</strong> to open Developer Tools → <strong>Network</strong> tab</li>
                <li>Reload the page (F5)</li>
                <li>Right-click the first request → <strong>Copy</strong> → <strong>Copy as cURL (bash)</strong></li>
                <li>Paste below and submit</li>
            </ol>
            <form hx-post="/api/youtube/setup" hx-target="#yt-setup-result" hx-swap="innerHTML">
                <textarea name="headers_raw" rows="6" placeholder="Paste cURL command or raw headers here..." style="font-family: monospace; font-size: 0.8rem;"></textarea>
                <button type="submit">Connect YouTube Music</button>
            </form>
            <div id="yt-setup-result"></div>
        </details>
    ''')


@router.post("/setup")
async def setup(headers_raw: str = Form(...)):
    success = yt.setup_from_headers(headers_raw)
    if success:
        return HTMLResponse('<p><mark>Connected!</mark> Reload the page to browse your library.</p>')
    return HTMLResponse('<p style="color: var(--pico-del-color);">Failed to connect. Make sure you copied the full request headers.</p>')


@router.get("/playlists")
async def playlists():
    result = yt.get_playlists()
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return result


@router.get("/playlists/html")
async def playlists_html(request: Request):
    result = yt.get_playlists()
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return templates.TemplateResponse(request=request, name="partials/yt_playlist_list.html",
                                      context={"playlists": result})


@router.get("/playlists/{playlist_id}")
async def playlist_tracks(
    playlist_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = yt.get_playlist_tracks(playlist_id)
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
    result = yt.get_playlist_tracks(playlist_id)
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
    result = yt.get_library_albums()
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return result


@router.get("/albums/html")
async def albums_html(request: Request):
    result = yt.get_library_albums()
    if result is None:
        return HTMLResponse('<p>Not connected to YouTube Music.</p>')
    return templates.TemplateResponse(request=request, name="partials/yt_albums.html",
                                      context={"albums": result})


@router.get("/albums/{browse_id}")
async def album_tracks(
    browse_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = yt.get_album_tracks(browse_id)
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
    result = yt.get_album_tracks(browse_id)
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
    result = yt.get_liked_songs()
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
    result = yt.get_liked_songs()
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
        return {"status": "already_downloaded", "track_id": existing.id}
    if existing and existing.status in ("pending", "downloading"):
        return {"status": "already_queued", "track_id": existing.id}

    quality = "mp3_320"
    if format == "flac":
        quality = "flac_lossy"
    elif format == "flac_lossless":
        quality = "flac_lossless"

    track = existing or Track(youtube_id=video_id)
    track.title = title
    track.artist = artist
    track.album = album
    track.track_number = track_number
    track.duration_ms = duration_ms
    track.artwork_url = image_url
    track.format = "flac" if "flac" in format else "mp3"
    track.quality = quality
    track.source = "youtube"
    track.status = "pending"
    track.error_message = None

    if not existing:
        session.add(track)
    await session.commit()
    await session.refresh(track)

    await download_worker.enqueue(track.id)
    return {"status": "queued", "track_id": track.id}


@router.post("/download/playlist/{playlist_id}")
async def download_playlist(
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

        quality = "mp3_320"
        if format == "flac":
            quality = "flac_lossy"
        elif format == "flac_lossless":
            quality = "flac_lossless"

        track = existing or Track(youtube_id=video_id)
        track.title = t["name"]
        track.artist = t.get("artist", "")
        track.album = t.get("album", "")
        track.duration_ms = t.get("duration_ms", 0)
        track.artwork_url = t.get("image_url", "")
        track.format = "flac" if "flac" in format else "mp3"
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
    return {"playlist": pl_name, "tracks_queued": len(queued), "details": queued}
