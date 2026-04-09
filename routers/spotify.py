from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import get_session
from models import Playlist, PlaylistTrack, Track
from services import spotify_client as sp
from worker import download_worker


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# --- HTML partial endpoints (for htmx) ---

@router.get("/status/html")
async def status_html(request: Request):
    if not settings.spotify_client_id:
        return templates.TemplateResponse(request=request, name="partials/spotify_status.html",
                                          context={"connected": False})
    connected = sp.is_connected()
    if connected:
        profile = sp.get_user_profile()
        return templates.TemplateResponse(request=request, name="partials/spotify_status.html",
                                          context={"connected": True, "user": profile.get("display_name", "Unknown") if profile else "Unknown"})
    return templates.TemplateResponse(request=request, name="partials/spotify_status.html",
                                      context={"connected": False, "auth_url": sp.get_auth_url()})


@router.get("/albums/html")
async def albums_html(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_saved_albums(limit=limit, offset=offset)
    if result is None:
        return HTMLResponse('<p>Not connected to Spotify. <a href="/api/spotify/auth">Connect now</a></p>')

    for album in result["albums"]:
        stmt = select(Track).where(Track.album == album["name"], Track.status == "done")
        res = await session.execute(stmt)
        album["downloaded_count"] = len(res.scalars().all())

    return templates.TemplateResponse(request=request, name="partials/album_grid.html",
                                      context={**result})


@router.get("/albums/{album_id}/html")
async def album_tracks_html(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_album_tracks(album_id)
    if result is None:
        return HTMLResponse('<p>Not connected to Spotify.</p>')

    for track in result["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == track["uri"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return templates.TemplateResponse(request=request, name="partials/album_tracks.html",
                                      context={**result})


@router.get("/playlists/html")
async def playlists_html(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    result = sp.get_playlists(limit=limit, offset=offset)
    if result is None:
        return HTMLResponse('<p>Not connected to Spotify. <a href="/api/spotify/auth">Connect now</a></p>')
    return templates.TemplateResponse(request=request, name="partials/playlist_list.html",
                                      context={**result})


@router.get("/playlists/{playlist_id}/html")
async def playlist_tracks_html(
    request: Request,
    playlist_id: str,
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_playlist_tracks(playlist_id)
    if result is None:
        return HTMLResponse('<p>Not connected to Spotify.</p>')
    if result.get("error"):
        return HTMLResponse(f'<p>Could not load playlist: {result["error"][:100]}</p>')

    for track in result["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == track["uri"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    playlist_ctx = result.get("playlist", {})
    playlist_ctx["id"] = playlist_id
    return templates.TemplateResponse(request=request, name="partials/playlist_tracks.html",
                                      context={"playlist": playlist_ctx, "tracks": result["tracks"]})


@router.get("/liked/html")
async def liked_html(
    request: Request,
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_liked_songs(limit=limit, offset=offset)
    if result is None:
        return HTMLResponse('<p>Not connected to Spotify. <a href="/api/spotify/auth">Connect now</a></p>')

    for track in result["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == track["uri"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return templates.TemplateResponse(request=request, name="partials/liked_tracks.html",
                                      context={**result})


# --- JSON API endpoints ---

@router.get("/status")
async def status():
    if not settings.spotify_client_id:
        return {"connected": False, "message": "Spotify credentials not configured. Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env"}
    connected = sp.is_connected()
    if connected:
        profile = sp.get_user_profile()
        return {"connected": True, "user": profile.get("display_name", "Unknown") if profile else "Unknown"}
    return {"connected": False, "auth_url": sp.get_auth_url()}


@router.get("/auth")
async def auth():
    if not settings.spotify_client_id:
        return {"error": "Spotify credentials not configured"}
    return RedirectResponse(sp.get_auth_url())


@router.get("/callback")
async def callback(code: str):
    sp.handle_callback(code)
    return RedirectResponse("/spotify")


@router.get("/albums")
async def albums(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_saved_albums(limit=limit, offset=offset)
    if result is None:
        return {"error": "Not connected"}

    for album in result["albums"]:
        stmt = select(Track).where(Track.album == album["name"], Track.status == "done")
        res = await session.execute(stmt)
        album["downloaded_count"] = len(res.scalars().all())

    return result


@router.get("/albums/{album_id}")
async def album_tracks(album_id: str, session: AsyncSession = Depends(get_session)):
    result = sp.get_album_tracks(album_id)
    if result is None:
        return {"error": "Not connected to Spotify"}

    for track in result["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == track["uri"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return result


@router.get("/playlists")
async def playlists(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    result = sp.get_playlists(limit=limit, offset=offset)
    if result is None:
        return {"error": "Not connected"}
    return result


@router.get("/playlists/{playlist_id}")
async def playlist_tracks(
    playlist_id: str,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_playlist_tracks(playlist_id, limit=limit, offset=offset)
    if result is None:
        return {"error": "Not connected to Spotify"}

    for track in result["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == track["uri"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return result


@router.get("/liked")
async def liked_songs(
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    result = sp.get_liked_songs(limit=limit, offset=offset)
    if result is None:
        return {"error": "Not connected"}

    for track in result["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == track["uri"])
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        track["download_status"] = existing.status if existing else None

    return result


@router.post("/download/track")
async def download_track(
    request: Request,
    uri: str,
    title: str,
    artist: str,
    album: str = "",
    track_number: int = 0,
    duration_ms: int = 0,
    artwork_url: str = "",
    artist_id: str = "",
    format: str = "mp3",
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Track).where(Track.spotify_uri == uri)
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

    # Fetch genre from artist if available
    genre = None
    if artist_id:
        genres = sp.get_artist_genres(artist_id)
        if genres:
            genre = ", ".join(genres[:3])  # Take top 3 genres

    quality = "mp3_320"
    if format == "flac":
        quality = "flac_lossy"
    elif format == "flac_lossless":
        quality = "flac_lossless"

    track = existing or Track(spotify_uri=uri)
    track.title = title
    track.artist = artist
    track.album = album
    track.genre = genre
    track.track_number = track_number
    track.duration_ms = duration_ms
    track.artwork_url = artwork_url
    track.format = "flac" if "flac" in format else "mp3"
    track.quality = quality
    track.source = "spotify"
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


@router.post("/download/album/{album_id}")
async def download_album(
    request: Request,
    album_id: str,
    format: str = "mp3",
    session: AsyncSession = Depends(get_session),
):
    album_data = sp.get_album_tracks(album_id)
    if album_data is None:
        return {"error": "Not connected to Spotify"}

    album = album_data["album"]
    queued = []

    # Fetch genre from album's primary artist
    album_genre = None
    album_genres = album.get("genres", [])
    if not album_genres:
        # Album genres are often empty; fetch from artist instead
        first_track = album_data["tracks"][0] if album_data["tracks"] else None
        if first_track:
            artist_genres = sp.get_artist_genres(first_track.get("artist_id", ""))
            if artist_genres:
                album_genre = ", ".join(artist_genres[:3])
    else:
        album_genre = ", ".join(album_genres[:3])

    for t in album_data["tracks"]:
        stmt = select(Track).where(Track.spotify_uri == t["uri"])
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing and existing.status in ("done", "pending", "downloading"):
            queued.append({"track_id": existing.id, "status": existing.status})
            continue

        quality = "mp3_320"
        if format == "flac":
            quality = "flac_lossy"
        elif format == "flac_lossless":
            quality = "flac_lossless"

        # Extract year from release_date (format: YYYY or YYYY-MM-DD)
        release_date = album.get("release_date", "")
        year = int(release_date[:4]) if release_date and len(release_date) >= 4 else None

        track = existing or Track(spotify_uri=t["uri"])
        track.title = t["name"]
        track.artist = t["artist"]
        track.album = album["name"]
        track.album_artist = album["artist"]
        track.track_number = t["track_number"]
        track.disc_number = t.get("disc_number")
        track.duration_ms = t["duration_ms"]
        track.artwork_url = album["image_url"]
        track.year = year
        track.genre = album_genre
        track.format = "flac" if "flac" in format else "mp3"
        track.quality = quality
        track.source = "spotify"
        track.status = "pending"
        track.error_message = None

        if not existing:
            session.add(track)
        await session.commit()
        await session.refresh(track)

        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    resp = {"album": album["name"], "tracks_queued": len(queued), "details": queued}
    if _is_htmx(request):
        # Re-render the album detail view with updated download statuses
        for track in album_data["tracks"]:
            stmt = select(Track).where(Track.spotify_uri == track["uri"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            track["download_status"] = existing.status if existing else None
        return templates.TemplateResponse(request=request, name="partials/album_tracks.html",
                                          context={"album": album, "tracks": album_data["tracks"]})
    return resp


@router.post("/download/playlist/{playlist_id}")
async def download_playlist(
    request: Request,
    playlist_id: str,
    format: str = "mp3",
    session: AsyncSession = Depends(get_session),
):
    playlist_data = sp.get_playlist_tracks(playlist_id)
    if playlist_data is None:
        return {"error": "Not connected to Spotify"}
    if playlist_data.get("error"):
        return {"error": playlist_data["error"]}

    pl_info = playlist_data.get("playlist", {})
    pl_name = pl_info.get("name", "Untitled Playlist")
    tracks_data = playlist_data["tracks"]

    if not tracks_data:
        return {"error": "No tracks in playlist"}

    # Create or update playlist in DB
    stmt = select(Playlist).where(Playlist.source == "spotify", Playlist.source_id == playlist_id)
    result = await session.execute(stmt)
    db_playlist = result.scalar_one_or_none()

    if not db_playlist:
        db_playlist = Playlist(name=pl_name, source="spotify", source_id=playlist_id,
                               image_url=pl_info.get("image_url"))
        session.add(db_playlist)
        await session.commit()
        await session.refresh(db_playlist)
    else:
        db_playlist.name = pl_name
        # Clear old entries
        await session.execute(
            select(PlaylistTrack).where(PlaylistTrack.playlist_id == db_playlist.id)
        )
        from sqlalchemy import delete
        await session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == db_playlist.id))
        await session.commit()

    queued = []
    for position, t in enumerate(tracks_data):
        # Queue track for download
        stmt = select(Track).where(Track.spotify_uri == t["uri"])
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing and existing.status in ("done", "pending", "downloading"):
            # Add to playlist entry
            session.add(PlaylistTrack(playlist_id=db_playlist.id, track_id=existing.id, position=position))
            queued.append({"track_id": existing.id, "status": existing.status})
            continue

        quality = "mp3_320"
        if format == "flac":
            quality = "flac_lossy"
        elif format == "flac_lossless":
            quality = "flac_lossless"

        # Fetch genre from artist
        genre = None
        if t.get("artist_id"):
            genres = sp.get_artist_genres(t["artist_id"])
            if genres:
                genre = ", ".join(genres[:3])

        track = existing or Track(spotify_uri=t["uri"])
        track.title = t["name"]
        track.artist = t["artist"]
        track.album = t.get("album", "")
        track.track_number = t.get("track_number")
        track.disc_number = t.get("disc_number")
        track.duration_ms = t.get("duration_ms", 0)
        track.artwork_url = t.get("album_image_url", "")
        track.genre = genre
        track.format = "flac" if "flac" in format else "mp3"
        track.quality = quality
        track.source = "spotify"
        track.status = "pending"
        track.error_message = None

        if not existing:
            session.add(track)
        await session.commit()
        await session.refresh(track)

        # Add to playlist entry
        session.add(PlaylistTrack(playlist_id=db_playlist.id, track_id=track.id, position=position))
        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    await session.commit()
    resp = {"playlist": pl_name, "tracks_queued": len(queued), "details": queued}
    if _is_htmx(request):
        # Re-render playlist tracks with updated statuses
        for t in tracks_data:
            stmt = select(Track).where(Track.spotify_uri == t["uri"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            t["download_status"] = existing.status if existing else None
        return templates.TemplateResponse(request=request, name="partials/playlist_tracks.html",
                                          context={"playlist": {"name": pl_name, "id": playlist_id},
                                                   "tracks": tracks_data})
    return resp
