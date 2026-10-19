"""The Saved tab: what the owner saved on Spotify, Qobuz and YouTube Music, read from the local snapshot, and
its downloads. Every GET only reads (an album opened for the first time fetches its tracklist once); refreshes,
downloads and batch controls are POSTs."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from models_saved import SavedItem
from services import album_view, saved_bulk, saved_download, saved_listing, saved_tracklists
from services import spotify_client as sp
from services.formats import resolve_format
from services.saved_bulk import BatchRefused
from services.saved_listing import ListFilters
from services.saved_store import KINDS, SOURCES
from services.saved_sync import LABELS, RefreshRefused, saved_sync, when_label
from worker import download_worker

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def list_filters(source: str = "all", kind: str = "track", missing: str = "0", other_album: str = "0",
                 removed: str = "0", q: str = "", sort: str = "added", page: int = Query(1),
                 per: int = Query(saved_listing.PER_PAGE)) -> ListFilters:
    return ListFilters.parse(source=source, kind=kind, missing=missing, other_album=other_album, removed=removed, q=q,
                             sort=sort, page=page, per=per)


def _choices() -> dict:
    return {"sources": SOURCES, "kinds": KINDS, "sorts": saved_listing.SORTS, "labels": LABELS}


@router.get("")
async def saved_view(request: Request, filters: ListFilters = Depends(list_filters),
                     session: AsyncSession = Depends(get_session)):
    status = await saved_sync.status()
    listing = await saved_listing.list_page(session, filters)
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/saved_view.html", context={
            "status": status, "listing": listing, "filters": listing["filters"], **_choices()})
    return {"status": status, "list": listing}


@router.get("/list")
async def saved_list(request: Request, filters: ListFilters = Depends(list_filters),
                     session: AsyncSession = Depends(get_session)):
    listing = await saved_listing.list_page(session, filters)
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/saved_list.html", context={
            "listing": listing, "filters": listing["filters"], **_choices()})
    return listing


@router.get("/status")
async def saved_status():
    return await saved_sync.status()


@router.get("/status/html")
async def saved_status_html(request: Request):
    return templates.TemplateResponse(request=request, name="partials/saved_status.html",
                                      context={"status": await saved_sync.status(), "refused": None})


def refused_message(source: str, refused: RefreshRefused) -> str:
    label = LABELS[source]
    if refused.reason == "running":
        return f"{label} is being read already."
    if refused.reason == "rate_limited":
        if source == "spotify":
            return sp.rate_limit_message() or "Spotify is rate-limiting Leasure."
        return "Qobuz asked Leasure to slow down; it continues by itself in a few minutes."
    if refused.reason == "daily_budget":
        return "Leasure has used today's Spotify allowance for library reading. It continues tomorrow."
    if refused.reason == "signed_out":
        return "Qobuz sign-in needed: paste a new token on Home."
    if refused.reason == "forbidden":
        return "Qobuz is unavailable right now; Leasure tries again in about an hour."
    return f"{label} is not connected."


@router.post("/refresh")
async def saved_refresh(request: Request, source: str = Form(...), full: str = Form("0")):
    if source not in SOURCES:
        raise HTTPException(400, detail="Pick Spotify, Qobuz or YT Music to refresh.")
    refused = None
    try:
        await saved_sync.request_refresh(source, full=full.strip().lower() in ("1", "true", "on", "yes"))
    except RefreshRefused as e:
        refused = {"source": source, "reason": e.reason, "message": refused_message(source, e),
                   "resume_after": e.resume_after.isoformat() if e.resume_after else None,
                   "resume_label": when_label(e.resume_after)}
    status = await saved_sync.status()
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/saved_status.html",
                                          context={"status": status, "refused": refused})
    if refused:
        return JSONResponse({"detail": refused["message"], **refused}, status_code=409)
    return status


# --- detail ---


async def _detail(session: AsyncSession, item_id: int) -> dict:
    item = await session.get(SavedItem, item_id)
    if item is None:
        raise HTTPException(404, detail="This saved item is no longer in the snapshot.")
    notice = await saved_tracklists.ensure_album_entries(session, item)
    if item.kind == "playlist" and not item.readable:
        notice = f"{LABELS[item.source]} does not let Leasure read this playlist."
    elif item.kind == "playlist" and item.entries_fetched_at is None:
        notice = "Leasure has not read this playlist's songs yet; they arrive with the next refresh."
    context = (await saved_listing.annotate(session, [[item]]))[0]
    entries = await saved_listing.entries_context(session, item) if item.kind in ("album", "playlist") else []
    artist_items = await saved_listing.artist_items(session, item) if item.kind == "artist" else []
    key = context["local_album_key"] if item.kind == "album" else None
    return {"item": context, "entries": entries, "artist_items": artist_items, "notice": notice,
            "missing": sum(1 for e in entries if e["library"] is None and e["readable"]),
            "library_album_url": f"/api/albums/{key}/html" if key else None,
            "library_album_download_url": await _library_album_download_url(session, key),
            "download_url": None if item.kind == "artist" else f"/api/saved/download/item/{item.id}",
            "confirm_above": saved_download.DETAIL_NO_CONFIRM_MAX}


async def _library_album_download_url(session: AsyncSession, key: str | None) -> str | None:
    """Album-info's "Download missing" for a saved album that is partly in the library, when that view allows it."""
    if not key:
        return None
    view = await album_view.load_group(session, key)
    return f"/api/albums/{key}/download-missing" if view and album_view.bulk_download_allowed(view) else None


@router.get("/item/{item_id}")
async def saved_item(item_id: int, session: AsyncSession = Depends(get_session)):
    return await _detail(session, item_id)


@router.get("/item/{item_id}/html")
async def saved_item_html(request: Request, item_id: int, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(request=request, name="partials/saved_detail.html",
                                      context=await _detail(session, item_id))


# --- downloads ---


def _flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _check_format(fmt: str) -> None:
    try:
        resolve_format(fmt)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from None


async def _saved_item(session: AsyncSession, item_id: int) -> SavedItem:
    item = await session.get(SavedItem, item_id)
    if item is None:
        raise HTTPException(404, detail="This saved item is no longer in the snapshot.")
    if item.kind == "artist":
        raise HTTPException(400, detail="Followed artists have no download; open the artist and download its songs.")
    return item


def _queued_headers() -> dict:
    return {"HX-Trigger": "saved:queued"}


def _confirm(request: Request, plan: saved_download.Plan, *, item_id: int | None, filters: dict | None,
             stale: bool = False, status_code: int = 200):
    view = {**saved_download.plan_view(plan), "item_id": item_id, "filters": filters, "stale": stale}
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/saved_confirm.html", context=view,
                                          status_code=status_code)
    return JSONResponse({"confirm": view}, status_code=status_code)


@router.post("/download/item/{item_id}")
async def download_item(request: Request, item_id: int, format: str = Form("native"), other_album: str = Form("0"),
                        session: AsyncSession = Depends(get_session)):
    """A song queues now; an album or playlist queues its missing songs now up to DETAIL_NO_CONFIRM_MAX, and
    answers with the confirmation above that. The client sends only the id: the snapshot says the rest."""
    _check_format(format)
    item = await _saved_item(session, item_id)
    include = _flag(other_album)
    if item.kind == "track":
        result = await saved_download.queue_saved(session, await saved_download.song_picks(session, item), format,
                                                  download_worker.enqueue, include_other_album=include)
        saved_sync.request_rematch()
        if _is_htmx(request):
            return templates.TemplateResponse(request=request, name="partials/download_badge.html",
                                              context=result.badge(), headers=_queued_headers())
        return {**result.summary(), **result.badge()}
    notice = await saved_tracklists.ensure_album_entries(session, item)
    if item.entries_fetched_at is None:
        raise HTTPException(409, detail=notice or "Leasure has not read this playlist's songs yet.")
    selection = await saved_download.selection_for_item(session, item)
    plan = await saved_download.make_plan(session, selection, format, include_other_album=include)
    if plan.numbers["songs"] > saved_download.DETAIL_NO_CONFIRM_MAX:
        return _confirm(request, plan, item_id=item.id, filters=None)
    result = await saved_download.queue_saved(session, selection.picks, format, download_worker.enqueue,
                                              include_other_album=include)
    saved_sync.request_rematch()
    if _is_htmx(request):
        context = await _detail(session, item_id)
        context["notice"] = f"Queued {len(result.queued)} songs."
        return templates.TemplateResponse(request=request, name="partials/saved_detail.html", context=context,
                                          headers=_queued_headers())
    return result.summary()


async def _selection(session: AsyncSession, filters: ListFilters, item_id: int | None) -> saved_download.Selection:
    if item_id is not None:
        return await saved_download.selection_for_item(session, await _saved_item(session, item_id))
    if filters.kind == "artist":
        raise HTTPException(400, detail="Followed artists have no download; pick Songs, Albums or Playlists.")
    return await saved_download.selection_for_filters(session, filters)


@router.get("/download/preview")
async def download_preview(request: Request, filters: ListFilters = Depends(list_filters), item_id: int | None = None,
                           format: str = "native", session: AsyncSession = Depends(get_session)):
    """The confirmation of a Download all (read-only): counts, size, days, and the `selection` token to confirm."""
    _check_format(format)
    selection = await _selection(session, filters, item_id)
    plan = await saved_download.make_plan(session, selection, format, include_other_album=filters.other_album)
    return _confirm(request, plan, item_id=item_id, filters=filters.params())


def _form_filters(form) -> ListFilters:
    return ListFilters.parse(**{name: form[name] for name in ("source", "kind", "missing", "other_album", "q", "sort")
                                if name in form})


@router.post("/download/all")
async def download_all(request: Request, session: AsyncSession = Depends(get_session)):
    """Creates the batch the confirmation showed; numbers that changed since (a stale `selection`) re-render the
    confirmation and queue nothing."""
    form = await request.form()
    fmt = str(form.get("format") or "native")
    _check_format(fmt)
    filters = _form_filters(form)
    item_id = int(form["item_id"]) if str(form.get("item_id") or "").isdigit() else None
    selection = await _selection(session, filters, item_id)
    plan, batch = await saved_download.start_batch(session, selection, fmt, include_other_album=filters.other_album,
                                                   token=str(form.get("selection") or ""))
    if batch is None:
        stale = plan.refused is None
        return _confirm(request, plan, item_id=item_id, filters=filters.params(), stale=stale,
                        status_code=200 if _is_htmx(request) else 409)
    saved_sync.request_rematch()
    return await _batches_response(request, session, notice=f"Download all queued {plan.numbers['total']:,} songs.",
                                   headers=_queued_headers())


async def _batches_response(request: Request, session: AsyncSession, *, notice: str | None = None,
                            headers: dict | None = None):
    batches = await saved_bulk.batch_summaries(session, with_failures=True)
    if _is_htmx(request):
        return templates.TemplateResponse(request=request, name="partials/saved_batches.html",
                                          context={"batches": batches, "notice": notice}, headers=headers)
    return JSONResponse({"batches": batches, "notice": notice}, headers=headers)


@router.get("/batches")
async def batches_json(request: Request, session: AsyncSession = Depends(get_session)):
    return await _batches_response(request, session)


@router.get("/batches/html")
async def batches_html(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(request=request, name="partials/saved_batches.html", context={
        "batches": await saved_bulk.batch_summaries(session, with_failures=True), "notice": None})


_CONTROLS = {"pause": saved_bulk.pause, "resume": saved_bulk.resume, "cancel": saved_bulk.cancel,
             "retry-failed": saved_bulk.retry_failed}
_DONE = {"pause": "Paused.", "resume": "Resumed.", "cancel": "Cancelled; nothing was deleted.",
         "retry-failed": "Failed songs wait again."}


async def _control(request: Request, session: AsyncSession, action: str, batch_id: int, item_id: int | None = None):
    try:
        if item_id is None:
            await _CONTROLS[action](session, batch_id)
        else:
            await saved_bulk.retry_failed(session, batch_id, item_id)
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from None
    except BatchRefused as e:
        if not _is_htmx(request):
            raise HTTPException(409, detail=str(e)) from None
        return await _batches_response(request, session, notice=str(e))
    return await _batches_response(request, session, notice=_DONE[action])


@router.post("/batches/{batch_id}/{action}")
async def batch_control(request: Request, batch_id: int, action: str, session: AsyncSession = Depends(get_session)):
    """pause | resume | cancel | retry-failed"""
    if action not in _CONTROLS:
        raise HTTPException(404, detail="Unknown action.")
    return await _control(request, session, action, batch_id)


@router.post("/batches/{batch_id}/items/{item_id}/retry")
async def batch_item_retry(request: Request, batch_id: int, item_id: int, session: AsyncSession = Depends(get_session)):
    return await _control(request, session, "retry-failed", batch_id, item_id)
