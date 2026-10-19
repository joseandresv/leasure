"""Local albums: the Library's Albums grid, the album view built from the library first, its
background album-info lookups and the downloads of songs the library is missing."""

import asyncio
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models import Track
from routers.art import _allowed as proxy_allows
from services import album_info, album_view, local_albums
from services.artwork import sidecar_art_path
from services.download_lock import DOWNLOAD_LOCK
from services.formats import resolve_format
from services.local_albums import LOOSE_KEY, LocalAlbum
from worker import download_worker

router = APIRouter()
templates = Jinja2Templates(directory="templates")

GONE_HTML = '<p class="detail-note">This album is no longer in your library.</p>'
STILL_LOOKING = "Still looking — reopen this album later."
BAD_PIN_LINK = "Paste a MusicBrainz release link or a Deezer album link."
SORTS = ("artist", "album", "year", "recent", "size")
COVER_HEADERS = {"Cache-Control": "public, max-age=3600"}


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _check_key(key: str) -> None:
    if not local_albums.valid_key(key):
        raise HTTPException(404, detail="No such album.")


def _card(album: LocalAlbum, sources: dict[str, str]) -> dict:
    loose = album.kind == "loose"
    return {
        "key": album.key, "kind": album.kind, "name": album.name, "artist": album.artist, "year": album.year,
        "track_count": album.track_count, "total_tracks": album.total_tracks,
        "cover_url": None if loose or not album.cover_track_id
        else f"/api/albums/{album.key}/cover?t={album.cover_track_id}",
        "quality_mix": album.quality_mix, "all_lossless": album.all_lossless, "info_source": sources.get(album.key),
        "downloaded_at": album.downloaded_at.isoformat() if album.downloaded_at else None,
    }


def _sorted(albums: list[LocalAlbum], sort: str) -> list[LocalAlbum]:
    by_artist = sorted(albums, key=lambda a: (a.artist.casefold(), a.year or 0, a.name.casefold()))
    if sort == "album":
        ordered = sorted(by_artist, key=lambda a: a.name.casefold())
    elif sort == "year":
        ordered = sorted(by_artist, key=lambda a: (a.year is None, -(a.year or 0)))
    elif sort == "recent":
        ordered = sorted(by_artist, key=lambda a: (a.downloaded_at is None, -(a.downloaded_at.timestamp())
                                                   if a.downloaded_at else 0))
    elif sort == "size":
        ordered = sorted(by_artist, key=lambda a: -a.track_count)
    else:
        ordered = by_artist
    return sorted(ordered, key=lambda a: a.kind == "loose")


async def _album_cards(session: AsyncSession, q: str, sort: str) -> list[dict]:
    albums = await local_albums.scan(session)
    needle = q.strip().casefold()
    if needle:
        albums = [a for a in albums if needle in f"{a.name} {a.artist}".casefold()]
    sources = await album_info.info_sources(session)
    return [_card(album, sources) for album in _sorted(albums, sort if sort in SORTS else "artist")]


@router.get("")
async def list_albums(q: str = "", sort: str = "artist", offset: int = 0, limit: int = 120,
                      session: AsyncSession = Depends(get_session)):
    cards = await _album_cards(session, q, sort)
    offset, limit = max(offset, 0), min(max(limit, 1), 500)
    return {"total": len(cards), "albums": cards[offset:offset + limit]}


@router.get("/html")
async def list_albums_html(request: Request, q: str = "", sort: str = "artist", offset: int = 0, limit: int = 120,
                           session: AsyncSession = Depends(get_session)):
    cards = await _album_cards(session, q, sort)
    offset, limit = max(offset, 0), min(max(limit, 1), 500)
    next_offset = offset + limit if offset + limit < len(cards) else None
    return templates.TemplateResponse(request=request, name="partials/library_albums.html", context={
        "albums": cards[offset:offset + limit], "q": q, "sort": sort, "total": len(cards), "offset": offset,
        "limit": limit, "next_offset": next_offset})


def render_view(request: Request, context: dict, status_code: int = 200):
    return templates.TemplateResponse(request=request, name="partials/album_view.html", context=context,
                                      status_code=status_code)


@router.get("/{key}")
async def album_json(key: str, session: AsyncSession = Depends(get_session)):
    _check_key(key)
    context = await album_view.build_context(session, key)
    if context is None:
        raise HTTPException(404, detail="This album is no longer in your library.")
    return {"album": context["album"], "tracks": context["tracks"], "info": context["info"]}


@router.get("/{key}/html")
async def album_html(request: Request, key: str, session: AsyncSession = Depends(get_session)):
    """The local album view; reads the DB only (no network, no queueing, no writes)."""
    _check_key(key)
    context = await album_view.build_context(session, key)
    if context is None:
        return HTMLResponse(GONE_HTML)
    return render_view(request, context)


async def _hinted_sidecar(session: AsyncSession, key: str, track_id: int):
    row = (await session.execute(select(Track.album, Track.album_artist, Track.artist, Track.file_path, Track.status)
                                 .where(Track.id == track_id))).first()
    if row is None or row.status != "done" or local_albums.key_of(row.album, row.album_artist, row.artist) != key:
        return None
    return await asyncio.to_thread(sidecar_art_path, row.file_path)


@router.get("/{key}/cover")
async def album_cover(key: str, t: int | None = None, session: AsyncSession = Depends(get_session)):
    """The hinted song's sidecar, any song's sidecar, the cached release cover, else a proxied provider cover."""
    _check_key(key)
    if t is not None and (path := await _hinted_sidecar(session, key, t)):
        return FileResponse(path, media_type="image/jpeg", headers=COVER_HEADERS)
    loaded = await local_albums.load(session, key)
    if loaded is None:
        raise HTTPException(404, detail="This album is no longer in your library.")
    if path := await asyncio.to_thread(local_albums.cover_file, key, loaded[1]):
        return FileResponse(path, media_type="image/jpeg", headers=COVER_HEADERS)
    url = next((row.artwork_url for row in loaded[1] if row.artwork_url and proxy_allows(row.artwork_url)), None)
    if url:
        return RedirectResponse(f"/api/art/proxy?url={quote(url, safe='')}", status_code=307)
    raise HTTPException(404, detail="This album has no cover art.")


async def _require_group(session: AsyncSession, key: str) -> None:
    _check_key(key)
    if await local_albums.load(session, key) is None:
        raise HTTPException(404, detail="This album is no longer in your library.")


@router.post("/{key}/info/ensure")
async def info_ensure(key: str, session: AsyncSession = Depends(get_session)):
    await _require_group(session, key)
    result = await album_info.ensure(session, key)
    return Response(status_code=202 if result == "pending" else 204)


@router.get("/{key}/info/status")
async def info_status(key: str):
    """204 while a lookup runs, 286 (stop polling) after STATUS_CAP_S, 200 + album-info:ready once settled."""
    _check_key(key)
    age = album_info.pending_for(key)
    if age is not None:
        if age < album_info.STATUS_CAP_S:
            return Response(status_code=204)
        return HTMLResponse(STILL_LOOKING, status_code=286)
    return Response(status_code=200, headers={"HX-Trigger": json.dumps({"album-info:ready": {"key": key}})})


_ENSURE_NOTICES = {"disabled": "Album info lookups are turned off.", "busy": "Lookups are busy, try again in a minute."}


async def _queue_and_render(request: Request, session: AsyncSession, key: str, *, pin=None, notice: str | None = None):
    result = await album_info.ensure(session, key, force=True, pin=pin)
    context = await album_view.build_context(session, key, notice=notice or _ENSURE_NOTICES.get(result),
                                             status_override="busy" if result == "busy" else None)
    if context is None:
        return HTMLResponse(GONE_HTML)
    return render_view(request, context)


@router.post("/{key}/info/refresh")
async def info_refresh(request: Request, key: str, session: AsyncSession = Depends(get_session)):
    await _require_group(session, key)
    return await _queue_and_render(request, session, key)


@router.post("/{key}/info/reject")
async def info_reject(request: Request, key: str, session: AsyncSession = Depends(get_session)):
    await _require_group(session, key)
    await album_info.reject(session, key)
    return await _queue_and_render(request, session, key)


@router.post("/{key}/info/pin")
async def info_pin(request: Request, key: str, url: str = Form(""), session: AsyncSession = Depends(get_session)):
    await _require_group(session, key)
    pin = album_info.parse_release_url(url)
    if pin is None:
        if _is_htmx(request):
            # Answered as a 200 into the form's own error slot: the typed link stays and no 4xx reaches the console.
            return HTMLResponse(BAD_PIN_LINK, headers={"HX-Retarget": f"#album-view-{key} .album-pin-error",
                                                       "HX-Reswap": "innerHTML"})
        raise HTTPException(400, detail=BAD_PIN_LINK)
    return await _queue_and_render(request, session, key, pin=pin)


def _badge(request: Request, result: dict, status_code: int = 200):
    # htmx swaps no 4xx, so a refusal reaches the row button as a 200 error badge.
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/download_badge.html", context=result)
    if status_code >= 400:
        raise HTTPException(status_code, detail=result.get("message", ""))
    return result


@router.post("/{key}/download/{disc}/{position}")
async def download_song(request: Request, key: str, disc: int, position: int, format: str = Form("native"),
                        session: AsyncSession = Depends(get_session)):
    """Queue one song of the stored release; the server builds the row, the client sends only the format."""
    _check_key(key)
    async with DOWNLOAD_LOCK:
        view = await album_view.load_group(session, key)
        if view is None:
            raise HTTPException(404, detail="This album is no longer in your library.")
        if not view.downloads_allowed:
            return _badge(request, {"status": "error", "message": "Album info is not confirmed for this album."}, 409)
        row = view.at(disc, position)
        if row is None:
            raise HTTPException(404, detail="That song is not on this album's tracklist.")
        try:
            quality, container = resolve_format(format)
        except ValueError as e:
            return _badge(request, {"status": "error", "message": str(e)}, 400)
        return _badge(request, await album_view.queue_row(session, view, row, quality, container,
                                                          download_worker.enqueue))


@router.post("/{key}/download-missing")
async def download_missing(request: Request, key: str, format: str = Form("native"),
                           session: AsyncSession = Depends(get_session)):
    """Queue every song the library lacks and retry this album's failed ones, for a confident match only."""
    _check_key(key)
    if key == LOOSE_KEY:
        raise HTTPException(409, detail="Loose songs have no album to complete.")
    async with DOWNLOAD_LOCK:
        view = await album_view.load_group(session, key)
        if view is None:
            raise HTTPException(404, detail="This album is no longer in your library.")
        if not album_view.bulk_download_allowed(view):
            message = "Download missing needs a confident album match; download songs one by one instead."
            if _is_htmx(request):
                return render_view(request, album_view.context(view, notice=message), status_code=409)
            raise HTTPException(409, detail=message)
        try:
            quality, container = resolve_format(format)
        except ValueError as e:
            if _is_htmx(request):
                return render_view(request, album_view.context(view, notice=str(e)))
            raise HTTPException(400, detail=str(e)) from e
        details = [await album_view.queue_row(session, view, row, quality, container, download_worker.enqueue)
                   for row in view.view_rows if row.state in ("missing", "error")]
    queued = sum(d["status"] == "queued" for d in details)
    if not _is_htmx(request):
        return {"queued": queued, "details": details}
    context = await album_view.build_context(session, key, notice=f"Queued {queued} songs.")
    return render_view(request, context) if context else HTMLResponse(GONE_HTML)
