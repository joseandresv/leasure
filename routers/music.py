"""Unified music router — provider-agnostic browsing, search, and smart download."""

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Playlist, PlaylistTrack, Track
from services import album_view, library_match, local_albums, spotify_albums
from services import spotify_client as sp
from services import youtube_client as yt
from services.formats import resolve_format
from services.library_match import TrackKeys
from services.music_aggregator import (
    enrich_with_download_status,
    find_youtube_album,
    find_youtube_album_by_name,
    get_unified_albums,
    get_unified_playlists,
    get_unified_recent,
    get_unique_artists,
    known_album,
)
from services.search import search_all
from services.tracks import optional_int, refresh_album_track, upsert_track
from worker import download_worker

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _format_error(request: Request, error: ValueError):
    """A format the app no longer offers: 400 for API callers, a badge htmx can swap."""
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                          context={"status": "error", "message": str(error)})
    raise HTTPException(status_code=400, detail=str(error))


def _unavailable(kind: str, provider: str, status: int | None = None) -> HTMLResponse:
    if provider == "spotify" and status == 403:
        return HTMLResponse("<p>This Spotify playlist is private or not available to Leasure "
                            "(Spotify only lets apps read playlists you own or follow).</p>")
    seconds = sp.rate_limit_seconds_left() if provider == "spotify" else 0
    if not seconds:
        return HTMLResponse(f"<p>{kind} not found or source unavailable.</p>")
    return HTMLResponse(f"<p>Spotify is rate-limiting Leasure right now (try again in {sp.wait_text(seconds)}). "
                        f"YouTube Music {kind.lower()}s still work.</p>")


def _spotify_down() -> str:
    return sp.rate_limit_message() or "Spotify did not answer"


def _youtube_stand_in(spotify_album: dict) -> dict:
    """Template context of a YouTube Music album shown in place of `spotify_album` ({"id", "name"})."""
    return {"notice": f"{_spotify_down()}, so this album comes from YouTube Music.", "spotify_album": spotify_album}


# --- Browse endpoints (return HTML partials) ---


async def _enrich_deck_tracks(tracks, session):
    """Single DB pass per track: set download_status + the richer `db` readout state.
    Shared by the Deck and Recently-Listened tabs (both render the track wheel)."""
    for track in tracks:
        existing = None
        for src in track.get("sources", []):
            if src["provider"] == "spotify" and src.get("uri"):
                existing = (await session.execute(
                    select(Track).where(Track.spotify_uri == src["uri"]))).scalar_one_or_none()
            elif src["provider"] in ("youtube", "youtube music") and src.get("id"):
                existing = (await session.execute(
                    select(Track).where(Track.youtube_id == src["id"]))).scalar_one_or_none()
            if existing:
                break
        track["download_status"] = existing.status if existing else None
        track["db"] = {
            "status": existing.status,
            "file_size": existing.file_size,
            "synced": bool(existing.synced_at),
            "engine": existing.engine_used,
            "genre": existing.genre,
            "year": existing.year,
            "isrc": existing.isrc,
            **existing.quality_readout(),
        } if existing else None


@router.get("/recent")
async def recent(request: Request, refresh: bool = False, session: AsyncSession = Depends(get_session)):
    """Recently listened tracks — rendered as the vertical deck roulette."""
    tracks = await asyncio.to_thread(get_unified_recent, 50, refresh)
    await _enrich_deck_tracks(tracks, session)
    return templates.TemplateResponse(
        request=request,
        name="partials/rhythm_deck.html",
        context={"tracks": tracks, "spotify_notice": sp.rate_limit_message()},
    )


@router.get("/deck")
async def deck(request: Request, refresh: bool = False, session: AsyncSession = Depends(get_session)):
    """THE DECK — recent tracks as a DDR-style banner wheel with a HiFi 'now selected' display."""
    tracks = await asyncio.to_thread(get_unified_recent, 50, refresh)  # match /recent so they share the cache
    await _enrich_deck_tracks(tracks, session)
    return templates.TemplateResponse(
        request=request,
        name="partials/rhythm_deck.html",
        context={"tracks": tracks, "spotify_notice": sp.rate_limit_message()},
    )


@router.get("/albums")
async def albums(request: Request, session: AsyncSession = Depends(get_session)):
    """All albums from all sources, merged."""
    album_list = await asyncio.to_thread(get_unified_albums)

    # Enrich with download counts
    for album in album_list:
        stmt = select(Track).where(Track.album == album["name"], Track.status == "done")
        result = await session.execute(stmt)
        album["downloaded_count"] = len(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="partials/music_albums.html",
        context={"albums": album_list, "spotify_notice": sp.rate_limit_message()},
    )


@router.get("/playlists")
async def playlists(request: Request):
    """All playlists from all sources."""
    playlist_list = await asyncio.to_thread(get_unified_playlists)
    return templates.TemplateResponse(
        request=request,
        name="partials/music_playlists.html",
        context={"playlists": playlist_list, "spotify_notice": sp.rate_limit_message()},
    )


@router.get("/artists")
async def artists(request: Request):
    """Unique artists from all sources."""
    artist_list = await asyncio.to_thread(get_unique_artists)
    return templates.TemplateResponse(
        request=request,
        name="partials/music_artists.html",
        context={"artists": artist_list, "spotify_notice": sp.rate_limit_message()},
    )


@router.get("/album/resolve")
async def album_resolve(
    request: Request,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration_ms: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Album detail for a track that carries no album id, found on YouTube Music by title and artist."""
    title, artist, album = title.strip(), artist.strip(), album.strip()
    browse_id = None
    if title or album:
        browse_id = await asyncio.to_thread(find_youtube_album, title, artist, album, duration_ms)
    if not browse_id:
        view = await _library_view(request, session, "", album, artist, notice="Found in your library.")
        return view or HTMLResponse("<p>No matching album found on YouTube Music.</p>")
    return await _album_detail_response(request, "youtube", browse_id, session)


@router.get("/album/{provider}/{album_id}")
async def album_detail(
    request: Request,
    provider: str,
    album_id: str,
    name: str = "",
    artist: str = "",
    session: AsyncSession = Depends(get_session),
):
    """Album detail with tracks from a specific provider. `name`/`artist` let a Spotify album
    open from YouTube Music or the library while Spotify cannot answer."""
    return await _album_detail_response(request, provider, album_id, session, name=name, artist=artist)


def _track_keys(provider: str, track: dict, album_name: str, album_aliases: tuple[str, ...] = ()) -> TrackKeys:
    return TrackKeys(title=track.get("name") or "", artist=track.get("artist") or "", album=album_name or "",
                     album_aliases=album_aliases,
                     youtube_id=track.get("id") if provider == "youtube" else None,
                     spotify_uri=track.get("uri") if provider == "spotify" else None,
                     isrc=track.get("isrc"))


def _sources(provider: str, track: dict) -> list[dict]:
    if provider == "spotify":
        return [{"provider": "spotify", "id": track["id"], "uri": track["uri"], "artist_id": track.get("artist_id", "")}]
    return [{"provider": "youtube", "id": track.get("id", "")}]


async def _album_rows(session: AsyncSession, provider: str, album: dict, tracks: list[dict],
                      album_aliases: tuple[str, ...] = ()) -> list[dict]:
    """Provider tracks with the library row each one already is, matched under any key."""
    keys = [_track_keys(provider, t, album.get("name", ""), album_aliases) for t in tracks]
    index = await library_match.library_index(session, keys)
    rows = []
    for track, track_keys in zip(tracks, keys, strict=True):
        existing = index.find(track_keys)
        rows.append({
            **track,
            "sources": _sources(provider, track),
            "download_status": existing.status if existing else None,
            "db": existing.quality_readout() if existing else None,
        })
    return rows


def _render_album(request: Request, provider: str, album_id: str, album: dict, tracks: list[dict], **extra):
    context = {"album": album, "tracks": tracks, "provider": provider, "album_id": album_id,
               **{key: value for key, value in extra.items() if value}}
    return templates.TemplateResponse(request=request, name="partials/music_album_detail.html", context=context)


def _album_name(album_id: str, name: str, artist: str, provider: str = "spotify") -> tuple[str, str]:
    if not name.strip() and (known := known_album(provider, album_id)):
        name, artist = known["name"], known["artist"]
    return name.strip(), artist.strip()


async def _library_view(request: Request, session: AsyncSession, album_id: str, name: str, artist: str,
                        notice: str | None = None, retry_url: str | None = None):
    """The local album view of the library's copy of (name, artist), or None when the library has none."""
    found = await local_albums.find_key(session, name, artist) if name else None
    if found is None:
        return None
    key, exact = found
    context = await album_view.build_context(session, key, album_id=album_id, notice=notice, retry_url=retry_url)
    if context is None:
        return None
    if not exact:
        shown = f"Showing your library's '{context['album']['name']}'."
        context["notice"] = f"{context['notice']} {shown}" if context.get("notice") else shown
    return templates.TemplateResponse(request=request, name="partials/album_view.html", context=context)


async def _spotify_album_fallback(request: Request, album_id: str, session: AsyncSession, name: str, artist: str):
    """A Spotify album Spotify cannot serve: the same record from YouTube Music, else the library's copy."""
    name, artist = _album_name(album_id, name, artist)
    browse_id = await asyncio.to_thread(find_youtube_album_by_name, name, artist) if name else None
    data = await asyncio.to_thread(yt.get_album_tracks, browse_id) if browse_id else None
    if data:
        # The library keeps the Spotify edition's name, which YouTube Music often words differently.
        rows = await _album_rows(session, "youtube", data["album"], data["tracks"], (name,))
        return _render_album(request, "youtube", browse_id, data["album"], rows,
                             **_youtube_stand_in({"id": album_id, "name": name}))
    view = await _library_view(
        request, session, album_id, name, artist,
        notice=f"{_spotify_down()}, so this shows your library's copy of this album.",
        retry_url=album_view.album_detail_url("spotify", album_id, name, artist))
    return view or _unavailable("Album", "spotify")


async def _album_detail_response(request: Request, provider: str, album_id: str, session: AsyncSession,
                                 name: str = "", artist: str = ""):
    data = None
    if provider == "spotify":
        data = await asyncio.to_thread(spotify_albums.get_album, album_id)
        if not data:
            return await _spotify_album_fallback(request, album_id, session, name, artist)
    elif provider == "youtube":
        data = await asyncio.to_thread(yt.get_album_tracks, album_id)
        if not data:
            view = await _library_view(request, session, album_id, *_album_name(album_id, name, artist, "youtube"),
                                       notice="YouTube Music did not answer, so this shows your library's copy.")
            return view or _unavailable("Album", provider)
    elif provider == "library":
        view = await _library_view(request, session, album_id, *_album_name(album_id, name, artist))
        return view or _unavailable("Album", "library")

    if not data:
        return _unavailable("Album", provider)
    rows = await _album_rows(session, provider, data["album"], data["tracks"])
    return _render_album(request, provider, album_id, data["album"], rows)


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
    error_status = None

    if provider == "spotify":
        data = await asyncio.to_thread(sp.get_playlist_tracks, playlist_id)
        error_status = data.get("error_status") if data else None
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
                    "db": existing.quality_readout() if existing else None,
                })

    elif provider == "youtube":
        data = await asyncio.to_thread(yt.get_playlist_tracks, playlist_id)
        if data:
            playlist_info = data.get("playlist", {})
            for t in data["tracks"]:
                existing = None
                if t.get("id"):
                    existing = (await session.execute(
                        select(Track).where(Track.youtube_id == t["id"]))).scalar_one_or_none()
                tracks.append({
                    **t,
                    "sources": [{"provider": "youtube", "id": t.get("id", "")}],
                    "download_status": existing.status if existing else None,
                    "db": existing.quality_readout() if existing else None,
                })

    if not playlist_info:
        return _unavailable("Playlist", provider, error_status)

    # Playlist row exists? (sync can only write an .m3u8 when this is True)
    pl_row_stmt = select(Playlist).where(Playlist.source == provider, Playlist.source_id == playlist_id)
    pl_row_result = await session.execute(pl_row_stmt)
    playlist_created = pl_row_result.scalar_one_or_none() is not None

    missing_count = sum(1 for t in tracks if t.get("download_status") != "done")

    return templates.TemplateResponse(
        request=request,
        name="partials/music_album_detail.html",
        context={"album": playlist_info, "tracks": tracks, "provider": provider,
                 "album_id": playlist_id, "is_playlist": True,
                 "playlist_created": playlist_created, "missing_count": missing_count},
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
        context={"results": results, "query": q, "spotify_notice": sp.rate_limit_message()},
    )


# --- Smart download ---


@router.post("/download/track")
async def download_track(
    request: Request,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    album_artist: str = Form(""),
    track_number: int = Form(0),
    disc_number: str = Form(""),
    total_tracks: str = Form(""),
    total_discs: str = Form(""),
    year: str = Form(""),
    duration_ms: int = Form(0),
    image_url: str = Form(""),
    spotify_uri: str = Form(""),
    spotify_artist_id: str = Form(""),
    youtube_id: str = Form(""),
    isrc: str = Form(""),
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

    try:
        quality, container = resolve_format(format)
    except ValueError as e:
        return _format_error(request, e)

    # Auto-pick source: prefer spotify (searches YouTube Music anyway), fallback to youtube
    source = "spotify" if spotify_uri else "youtube"

    # Fetch genre from Spotify artist if available
    genre = None
    if spotify_artist_id:
        genres = await asyncio.to_thread(sp.get_artist_genres, spotify_artist_id)
        if genres:
            genre = ", ".join(genres[:3])

    track = await upsert_track(
        session,
        {
            "spotify_uri": spotify_uri,
            "youtube_id": youtube_id,
            "isrc": isrc,
            "title": title,
            "artist": artist,
            "album": album,
            "album_artist": album_artist or None,
            "genre": genre,
            "track_number": track_number,
            "disc_number": optional_int(disc_number),
            "total_tracks": optional_int(total_tracks),
            "total_discs": optional_int(total_discs),
            "year": optional_int(year),
            "duration_ms": duration_ms,
            "artwork_url": image_url,
        },
        quality=quality, container=container, source=source, existing=existing,
    )

    await download_worker.enqueue(track.id)

    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                          context={"status": "queued"})
    return {"status": "queued", "track_id": track.id}


def _stand_in_for(provider: str, spotify_album_id: str, spotify_album_name: str) -> dict | None:
    if provider != "youtube" or not spotify_albums.is_album_id(spotify_album_id):
        return None
    return {"id": spotify_album_id, "name": _album_name(spotify_album_id, spotify_album_name, "")[0]}


@router.post("/download/album/{provider}/{album_id}")
async def download_album(
    request: Request,
    provider: str,
    album_id: str,
    format: str = Form("mp3"),
    spotify_album_id: str = Form(""),
    spotify_album_name: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Download all tracks from an album using the specified provider. `spotify_album_*` name the
    Spotify album a YouTube Music album stands in for while Spotify cannot answer."""
    album_data = None
    tracks_data = []

    if provider == "spotify":
        data = await asyncio.to_thread(spotify_albums.get_album, album_id)
        if data:
            album_data = data["album"]
            tracks_data = data["tracks"]
    elif provider == "youtube":
        data = await asyncio.to_thread(yt.get_album_tracks, album_id)
        if data:
            album_data = data["album"]
            tracks_data = data["tracks"]

    if not album_data:
        return HTMLResponse("<p>Album not found.</p>") if _is_htmx(request) else {"error": "Album not found"}

    try:
        quality, container = resolve_format(format)
    except ValueError as e:
        return _format_error(request, e)

    # Fetch genre
    album_genre = None
    if provider == "spotify":
        album_genres = album_data.get("genres", [])
        if not album_genres and tracks_data:
            artist_genres = await asyncio.to_thread(sp.get_artist_genres, tracks_data[0].get("artist_id", ""))
            if artist_genres:
                album_genre = ", ".join(artist_genres[:3])
        elif album_genres:
            album_genre = ", ".join(album_genres[:3])

    # Spotify dates the album with release_date, YouTube Music with a plain year.
    year = sp.release_year(album_data.get("release_date")) or optional_int(album_data.get("year"))

    stand_in_for = _stand_in_for(provider, spotify_album_id, spotify_album_name)
    aliases = (stand_in_for["name"],) if stand_in_for and stand_in_for["name"] else ()
    queued = []
    keys = [_track_keys(provider, t, album_data["name"], aliases) for t in tracks_data]
    index = await library_match.library_index(session, keys)
    for t, track_keys in zip(tracks_data, keys, strict=True):
        existing = index.by_source_id(track_keys)
        data = {
            "spotify_uri": t.get("uri") if provider == "spotify" else None,
            "youtube_id": t.get("id") if provider == "youtube" else None,
            "isrc": t.get("isrc"),
            "title": t["name"],
            "artist": t.get("artist", ""),
            "album": album_data["name"],
            "album_artist": album_data.get("album_artist") or album_data.get("artist") or None,
            "genre": album_genre,
            "track_number": t.get("track_number"),
            "disc_number": t.get("disc_number"),
            "total_tracks": album_data.get("total_tracks"),
            "total_discs": album_data.get("total_discs"),
            "duration_ms": t.get("duration_ms", 0),
            "year": year,
            "artwork_url": album_data.get("image_url", ""),
        }
        if existing and existing.status in ("done", "pending", "downloading"):
            stale = await refresh_album_track(session, existing, data)
            queued.append({"track_id": existing.id, "status": existing.status, "stale_device_path": stale})
            continue
        # The same song under another key (an earlier Spotify download seen from YouTube Music):
        # never a second copy, and not renumbered from this provider's album either.
        similar = existing or index.find(track_keys)
        if similar and similar.status in ("done", "pending", "downloading"):
            queued.append({"track_id": similar.id, "status": similar.status})
            continue

        track = await upsert_track(session, data, quality=quality, container=container, source=provider,
                                   existing=similar)

        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    if _is_htmx(request):
        rows = await _album_rows(session, provider, album_data, tracks_data, aliases)
        return _render_album(request, provider, album_id, album_data, rows,
                             **(_youtube_stand_in(stand_in_for) if stand_in_for else {}))

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
        data = await asyncio.to_thread(sp.get_playlist_tracks, playlist_id)
        if data and not data.get("error"):
            playlist_data = data.get("playlist", {})
            tracks_data = data["tracks"]
    elif provider == "youtube":
        data = await asyncio.to_thread(yt.get_playlist_tracks, playlist_id)
        if data:
            playlist_data = data.get("playlist", {})
            tracks_data = data.get("tracks", [])

    if not playlist_data:
        return HTMLResponse("<p>Playlist not found.</p>") if _is_htmx(request) else {"error": "Playlist not found"}

    try:
        quality, container = resolve_format(format)
    except ValueError as e:
        return _format_error(request, e)

    # Persist the playlist so device sync can generate an .m3u8 for it later.
    pl_name = playlist_data.get("name") or "Untitled Playlist"
    stmt = select(Playlist).where(Playlist.source == provider, Playlist.source_id == playlist_id)
    result = await session.execute(stmt)
    db_playlist = result.scalar_one_or_none()
    if not db_playlist:
        db_playlist = Playlist(name=pl_name, source=provider, source_id=playlist_id,
                               image_url=playlist_data.get("image_url"))
        session.add(db_playlist)
        await session.commit()
        await session.refresh(db_playlist)
    else:
        db_playlist.name = pl_name
        if playlist_data.get("image_url"):
            db_playlist.image_url = playlist_data["image_url"]
        # Clear old entries so positions/order stay fresh
        await session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == db_playlist.id))
        await session.commit()

    queued = []
    for position, t in enumerate(tracks_data):
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
            session.add(PlaylistTrack(playlist_id=db_playlist.id, track_id=existing.id, position=position))
            queued.append({"track_id": existing.id, "status": existing.status})
            continue

        track = await upsert_track(
            session,
            {
                "spotify_uri": t.get("uri") if provider == "spotify" else None,
                "youtube_id": t.get("id") if provider == "youtube" else None,
                "isrc": t.get("isrc"),
                "title": t["name"],
                "artist": t.get("artist", ""),
                "album": t.get("album") or playlist_data.get("name", ""),
                "album_artist": t.get("album_artist"),
                "track_number": t.get("track_number"),
                "disc_number": t.get("disc_number"),
                "total_tracks": t.get("total_tracks"),
                "duration_ms": t.get("duration_ms", 0),
                "year": t.get("year"),
                "artwork_url": (t.get("image_url") or t.get("album_image_url")
                                or playlist_data.get("image_url", "")),
            },
            quality=quality, container=container, source=provider, existing=existing,
        )

        session.add(PlaylistTrack(playlist_id=db_playlist.id, track_id=track.id, position=position))
        await download_worker.enqueue(track.id)
        queued.append({"track_id": track.id, "status": "queued"})

    await session.commit()

    if _is_htmx(request):
        tracks_with_status = []
        for t in tracks_data:
            ex = None
            if provider == "spotify" and t.get("uri"):
                ex = (await session.execute(
                    select(Track).where(Track.spotify_uri == t["uri"]))).scalar_one_or_none()
            elif provider == "youtube" and t.get("id"):
                ex = (await session.execute(
                    select(Track).where(Track.youtube_id == t["id"]))).scalar_one_or_none()
            tracks_with_status.append({
                **t,
                "sources": [{"provider": provider, "id": t.get("id", ""), "uri": t.get("uri", "")}],
                "download_status": ex.status if ex else None,
                "db": ex.quality_readout() if ex else None,
            })
        missing_count = sum(1 for t in tracks_with_status if t.get("download_status") != "done")
        return templates.TemplateResponse(
            request=request,
            name="partials/music_album_detail.html",
            context={"album": playlist_data, "tracks": tracks_with_status, "provider": provider,
                     "album_id": playlist_id, "is_playlist": True,
                     "playlist_created": True, "missing_count": missing_count},
        )

    return {"playlist": playlist_data.get("name", ""), "tracks_queued": len(queued), "details": queued}
