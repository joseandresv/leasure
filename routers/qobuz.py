"""Qobuz card (sign-in, check) and FLAC upgrade endpoints."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import get_session
from models import Track
from services import qobuz_client, qobuz_engine, qobuz_session
from services.yt_engine import DownloadFailed

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

MAX_TRACK_IDS = 500
TOKEN_REFUSED = "Qobuz refused this token — copy it again from play.qobuz.com."
SIGNED_OUT_NOTICE = "Leasure forgot this token. To end the session, sign out on play.qobuz.com."
NOT_CONNECTED = "Sign in to Qobuz first."
SESSION_CHANGED = {"HX-Trigger": "qobuz:changed"}


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _checked_label(checked_at: str | None) -> str | None:
    if not checked_at:
        return None
    moment = datetime.fromisoformat(checked_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if datetime.now(UTC) - moment < timedelta(minutes=1):
        return "just now"
    return moment.astimezone().strftime("%d %b %Y, %H:%M")


def _queued_trigger(track_ids: list[int]) -> dict[str, str]:
    return {"HX-Trigger": json.dumps({"qobuz:queued": {"track_ids": track_ids}})}


async def _card(request: Request, *, error: str | None = None, notice: str | None = None,
                check: dict | None = None, user_id_value: str = "", headers: dict | None = None):
    status = await asyncio.to_thread(qobuz_session.status)
    check = check or await asyncio.to_thread(qobuz_session.cached_check)
    return templates.TemplateResponse(
        request=request, name="partials/qobuz_status.html", headers=headers,
        context={"qobuz": status, "check": {**check, "checked_label": _checked_label(check.get("checked_at"))},
                 "expired": status["state"] == "expired", "error": error, "notice": notice,
                 "user_id_value": user_id_value})


@router.get("/status")
async def status():
    return await asyncio.to_thread(qobuz_session.status)


@router.get("/status/html")
async def status_html(request: Request):
    return await _card(request)


@router.post("/session")
async def sign_in(request: Request, user_id: str = Form(""), user_auth_token: str = Form(""),
                  localuser: str = Form("")):
    """Store a pasted Qobuz sign-in after Qobuz accepts it."""
    status_code, detail = 200, None
    try:
        if len(localuser) > qobuz_session.MAX_LOCALUSER_CHARS:
            raise qobuz_session.SessionInputError("The localuser value is too long.")
        parsed = qobuz_session.parse_credentials(user_id, user_auth_token, localuser)
        result = await asyncio.to_thread(qobuz_session.sign_in, *parsed)
    except qobuz_session.SessionInputError as e:
        status_code, detail = 400, str(e)
    except qobuz_client.QobuzAuthError:
        status_code, detail = 401, TOKEN_REFUSED
    except (qobuz_client.QobuzError, qobuz_session.SessionChanged) as e:
        status_code, detail = 502, qobuz_client.redact(str(e)) or "Qobuz did not answer — try again."
    if detail:
        logger.warning("Qobuz sign-in failed (%d): %s", status_code, detail)
        if _is_htmx(request):
            return await _card(request, error=detail, user_id_value=qobuz_session.echo_user_id(user_id))
        return JSONResponse({"detail": detail}, status_code=status_code)
    if _is_htmx(request):
        return await _card(request, headers=SESSION_CHANGED)
    return JSONResponse({"ok": True, "status": result}, headers=SESSION_CHANGED)


@router.delete("/session")
async def sign_out(request: Request):
    removed = await asyncio.to_thread(qobuz_session.clear)
    if _is_htmx(request):
        return await _card(request, notice=SIGNED_OUT_NOTICE, headers=SESSION_CHANGED)
    return JSONResponse({"ok": True, "removed": removed}, headers=SESSION_CHANGED)


@router.get("/check")
async def check():
    return await asyncio.to_thread(qobuz_session.cached_check)


@router.post("/check")
async def run_check(request: Request):
    result = await asyncio.to_thread(qobuz_session.run_check)
    if result["state"] in ("fail", "error"):
        logger.warning("Qobuz check %s: %s", result["state"], qobuz_client.redact(result.get("error") or "")
                       or "no lossless stream for the test track")
    if _is_htmx(request):
        return await _card(request, check=result)
    return result


async def _connected() -> bool:
    return await asyncio.to_thread(qobuz_session.usable) is not None


def _parse_ids(raw: str) -> list[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts or len(parts) > MAX_TRACK_IDS or not all(p.isdigit() for p in parts):
        raise HTTPException(status_code=400, detail=f"Send 1 to {MAX_TRACK_IDS} track ids, separated by commas.")
    return list(dict.fromkeys(int(p) for p in parts))


async def _done_tracks(session: AsyncSession, track_ids: list[int] | None) -> list[Track]:
    stmt = select(Track).where(Track.status == "done")
    if track_ids is not None:
        stmt = stmt.where(Track.id.in_(track_ids))
    return list((await session.scalars(stmt.order_by(Track.id))).all())


@router.get("/upgrade/summary")
async def upgrade_summary(session: AsyncSession = Depends(get_session)):
    tracks = await _done_tracks(session, None)
    return {
        "connected": await _connected(),
        "eligible": sum(t.upgrade_eligibility(include_unmatched=False) is None for t in tracks),
        "eligible_unmatched": sum(t.upgrade_eligibility(include_unmatched=True) is None for t in tracks),
        "queued": sum(t.upgrade_state == "queued" for t in tracks),
        "running": sum(t.upgrade_state == "downloading" for t in tracks),
        "failed": sum(t.upgrade_state == "error" for t in tracks),
    }


async def _button(request: Request, session: AsyncSession, scope: str, track_ids: list[int] | None, *,
                  message: str | None = None, error: str | None = None, headers: dict | None = None):
    stored = await asyncio.to_thread(qobuz_session.load)
    tracks = await _done_tracks(session, track_ids)
    eligible = [t.id for t in tracks if t.upgrade_eligibility(include_unmatched=False) is None]
    unmatched = [t.id for t in tracks if t.upgrade_eligibility(include_unmatched=False) is not None
                 and t.upgrade_eligibility(include_unmatched=True) is None]
    per_track = scope != "all"
    return templates.TemplateResponse(
        request=request, name="partials/qobuz_upgrade_button.html", headers=headers,
        context={"scope": scope, "connected": bool(stored and stored.state == "connected"),
                 "session_state": stored.state if stored else "not_configured",
                 "eligible_ids": eligible if per_track else [], "eligible_count": len(eligible),
                 "unmatched_ids": unmatched if per_track else [], "unmatched_count": len(unmatched),
                 "queued_count": sum(t.upgrade_state in ("queued", "downloading") for t in tracks),
                 "failed_count": sum(t.upgrade_state == "error" for t in tracks),
                 "keep_days": settings.qobuz_upgrade_keep_days, "daily_cap": settings.qobuz_max_downloads_per_day,
                 "message": message, "error": error})


@router.get("/upgrade/button/html")
async def upgrade_button(request: Request, scope: str = "album", track_ids: str = "",
                         session: AsyncSession = Depends(get_session)):
    if scope not in ("album", "all"):
        raise HTTPException(status_code=400, detail="scope must be album or all.")
    ids = None if scope == "all" else (_parse_ids(track_ids) if track_ids.strip() else [])
    return await _button(request, session, scope, ids)


def _queued_message(result: dict) -> str:
    count = len(result["queued"])
    if not count:
        return "Nothing to upgrade here."
    return f"Queued {count} track{'s' if count != 1 else ''} for a FLAC upgrade."


async def _queue(request: Request, session: AsyncSession, scope: str, track_ids: list[int] | None,
                 include_unmatched: bool):
    button_scope = "all" if scope == "all" else "album"
    if not await _connected():
        if _is_htmx(request):
            return await _button(request, session, button_scope, track_ids, error=NOT_CONNECTED)
        return JSONResponse({"detail": NOT_CONNECTED}, status_code=409)
    result = await qobuz_engine.queue_upgrades(session, track_ids, include_unmatched=include_unmatched)
    trigger = _queued_trigger(result["queued"])
    if _is_htmx(request):
        return await _button(request, session, button_scope, track_ids, message=_queued_message(result),
                             headers=trigger)
    return JSONResponse(result, headers=trigger)


@router.post("/upgrade/tracks")
async def upgrade_tracks(request: Request, track_ids: str = Form(""), scope: str = Form("album"),
                         include_unmatched: bool = Form(False), session: AsyncSession = Depends(get_session)):
    """Queue FLAC upgrades of the given finished tracks."""
    return await _queue(request, session, scope, _parse_ids(track_ids), include_unmatched=include_unmatched)


@router.post("/upgrade/all")
async def upgrade_all(request: Request, include_unmatched: bool = Form(False),
                      session: AsyncSession = Depends(get_session)):
    """Queue FLAC upgrades of every eligible finished track."""
    return await _queue(request, session, "all", None, include_unmatched=include_unmatched)


@router.post("/upgrade/track/{track_id}")
async def upgrade_track(request: Request, track_id: int, session: AsyncSession = Depends(get_session)):
    """Queue (or retry) the FLAC upgrade of one track, even one Qobuz could not match before."""
    from routers.downloads import _queue_row

    track = await session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="That track is no longer in the library.")
    if not await _connected():
        raise HTTPException(status_code=409, detail=NOT_CONNECTED)
    result = await qobuz_engine.queue_upgrades(session, [track_id], include_unmatched=True)
    trigger = _queued_trigger(result["queued"])
    if _is_htmx(request):
        await session.refresh(track)
        return templates.TemplateResponse(request=request, name="partials/queue_item.html",
                                          context={"t": _queue_row(track, True)}, headers=trigger)
    return JSONResponse({"queued": track_id in result["queued"], "reason": result["skipped"].get(track_id)},
                        headers=trigger)


@router.post("/upgrade/dismiss/{track_id}")
async def upgrade_dismiss(request: Request, track_id: int, session: AsyncSession = Depends(get_session)):
    """Clear a failed upgrade; the track keeps its current file."""
    track = await session.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="That track is no longer in the library.")
    if track.upgrade_state == "error":
        track.upgrade_state, track.upgrade_error = None, None
        await session.commit()
    if _is_htmx(request):
        return HTMLResponse("")
    return {"dismissed": True}


@router.post("/upgrade/revert/{track_id}")
async def upgrade_revert(request: Request, track_id: int, session: AsyncSession = Depends(get_session)):
    """Put back the file a FLAC upgrade replaced."""
    if not await session.get(Track, track_id):
        raise HTTPException(status_code=404, detail="That track is no longer in the library.")
    try:
        await qobuz_engine.revert_upgrade(track_id)
    except DownloadFailed as e:
        # The backup is gone for good, so the Undo button is replaced rather than left to fail again.
        if _is_htmx(request) and str(e) == qobuz_engine.NOT_REVERTIBLE:
            return HTMLResponse(f'<span class="badge-note" role="status">{escape(str(e))}</span>')
        raise HTTPException(status_code=409, detail=str(e)) from None
    trigger = _queued_trigger([track_id])
    if _is_htmx(request):
        return HTMLResponse('<span class="badge-note">Reverted — the previous file is back.</span>', headers=trigger)
    return JSONResponse({"reverted": True}, headers=trigger)
