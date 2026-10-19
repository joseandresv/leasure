"""The "Download all" lane: hands batch songs to the download queue one at a time, at most 20 an hour with a
2-5 minute gap after each, within its share of the daily caps, so clicks and FLAC upgrades never wait behind it."""

import asyncio
import json
import logging
import random
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from db import async_session
from models import Track, utc_now
from models_saved import SavedBatch, SavedBatchItem, SavedItem
from services import qobuz_engine, qobuz_session, saved_download, saved_tracklists
from services.downloader import bulk_youtube_room_left
from services.formats import resolve_format
from services.saved_download import BULK_GAP_S, BULK_MAX_PER_HOUR
from worker import BULK_PRIORITY

logger = logging.getLogger(__name__)

# "10-23" limits when bulk songs start (local hours, end exclusive); None: any time of day.
BULK_HOURS: str | None = None
FEEDING_STATES = ("expanding", "active")
EXPAND_RETRY_S = 600
SKIPS_PER_RUN = 200
FAILED_LISTED = 200

_lane: dict = {}
_expand_wait: dict[int, float] = {}
_feed_lock = asyncio.Lock()


def reset_for_tests() -> None:
    global _feed_lock
    _lane.clear()
    _lane.update(next_at=None, status="idle", blocked=None)
    _expand_wait.clear()
    _feed_lock = asyncio.Lock()


reset_for_tests()


def _epoch(value: datetime) -> float:
    return value.replace(tzinfo=UTC).timestamp()


def within_hours(window: str | None, hour: int) -> bool:
    if not window:
        return True
    start, end = (int(part) for part in window.split("-"))
    return start <= hour < end if start <= end else hour >= start or hour < end


async def reconcile() -> None:
    """Fed songs whose download finished (or was dismissed) are done or failed; finished batches close."""
    finished = False
    async with async_session() as session:
        rows = (await session.execute(select(SavedBatchItem, Track.status, Track.error_message)
                                      .outerjoin(Track, Track.id == SavedBatchItem.track_id)
                                      .where(SavedBatchItem.state == "fed"))).all()
        for item, status, error in rows:
            if status not in (None, "done", "error"):
                continue
            item.state = "done" if status == "done" else "error"
            item.note = None if status == "done" else (error or "Removed from the download queue.")[:300]
            item.finished_at, finished = utc_now(), True
        await session.flush()
        open_left = select(SavedBatchItem.batch_id).where(SavedBatchItem.state.in_(("waiting", "expand", "fed")))
        for batch in (await session.scalars(select(SavedBatch).where(SavedBatch.state.in_(FEEDING_STATES),
                                                                     SavedBatch.id.not_in(open_left)))).all():
            batch.state, batch.finished_at = "done", utc_now()
        await session.commit()
    if finished:
        _lane["next_at"] = time.time() + random.uniform(*BULK_GAP_S)


async def expand_next() -> bool:
    """Read one saved album a batch reached (one service call); True when it tried."""
    async with async_session() as session:
        candidates = (await session.scalars(
            select(SavedBatchItem).join(SavedBatch).where(SavedBatchItem.state == "expand",
                                                          SavedBatch.state.in_(FEEDING_STATES))
            .order_by(SavedBatchItem.batch_id, SavedBatchItem.position))).all()
        item = next((c for c in candidates if _expand_wait.get(c.id, 0) <= time.time()), None)
        if item is None:
            return False
        batch = await session.get(SavedBatch, item.batch_id)
        collection = await session.get(SavedItem, item.saved_item_id) if item.saved_item_id else None
        if collection is None:
            item.state, item.note = "error", "This album is no longer saved."
            await session.commit()
            return True
        if collection.entries_fetched_at is None:
            notice = await saved_tracklists.ensure_album_entries(session, collection, background=True)
            if collection.entries_fetched_at is None:
                _expand_wait[item.id] = time.time() + EXPAND_RETRY_S
                item.note = (notice or "")[:300] or None
                await session.commit()
                return True
        await saved_download.expand_into(session, batch, item, await saved_download.collection_picks(session, collection))
    return True


def _qobuz_blocker() -> str | None:
    """Why the lane cannot start a Qobuz song now: absent (no Qobuz account), signed_out, paused, cooldown,
    cap or share. Blocking."""
    stored = qobuz_session.load()
    if stored is None or stored.state == "ineligible":
        return "absent"
    if stored.state != "connected":
        return "signed_out"
    if qobuz_engine.breaker_state()["reason"]:
        return "paused"
    if qobuz_engine.cooldown_left():
        return "cooldown"
    if qobuz_engine.daily_cap_reached():
        return "cap"
    return "share" if qobuz_engine.share_left("bulk") == 0 else None


async def route_blockers() -> dict[str, str | None]:
    youtube = None if await asyncio.to_thread(bulk_youtube_room_left) else "youtube_cap"
    return {"qobuz": await asyncio.to_thread(_qobuz_blocker), "youtube": youtube}


def _feedable(blockers: dict) -> list:
    """Item conditions the open routes allow; Best without a Qobuz account goes to YouTube as MP3."""
    conditions = []
    if blockers["qobuz"] is None:
        conditions.append(SavedBatchItem.route == "qobuz")
    if blockers["youtube"] is None:
        conditions.append(SavedBatchItem.route == "youtube")
        if blockers["qobuz"] == "absent":
            conditions.append((SavedBatchItem.route == "qobuz") & (SavedBatchItem.quality == "native"))
    return conditions


async def _next_item(session, conditions: list) -> SavedBatchItem | None:
    """The first waiting song of the batch fed longest ago (round-robin across batches)."""
    last_fed = (select(SavedBatchItem.batch_id, func.max(SavedBatchItem.fed_at).label("fed"))
                .group_by(SavedBatchItem.batch_id).subquery())
    batches = (await session.execute(select(SavedBatch.id).outerjoin(last_fed, last_fed.c.batch_id == SavedBatch.id)
                                     .where(SavedBatch.state.in_(FEEDING_STATES))
                                     .order_by(last_fed.c.fed.is_not(None), last_fed.c.fed, SavedBatch.id))).scalars().all()
    for batch_id in batches:
        item = await session.scalar(select(SavedBatchItem).where(
            SavedBatchItem.batch_id == batch_id, SavedBatchItem.state == "waiting", or_(*conditions))
            .order_by(SavedBatchItem.position, SavedBatchItem.id).limit(1))
        if item is not None:
            return item
    return None


async def _waiting_exists(session) -> bool:
    return await session.scalar(select(SavedBatchItem.id).join(SavedBatch).where(
        SavedBatchItem.state == "waiting", SavedBatch.state.in_(FEEDING_STATES)).limit(1)) is not None


async def _pace_blocker(session) -> str | None:
    """busy (a song in flight, deferred ones included), gap, hourly or hours."""
    if await session.scalar(select(SavedBatchItem.id).where(SavedBatchItem.state == "fed").limit(1)):
        return "busy"
    if _lane["next_at"] is None:
        last = await session.scalar(select(func.max(SavedBatchItem.finished_at)).where(SavedBatchItem.fed_at.is_not(None)))
        _lane["next_at"] = _epoch(last) + BULK_GAP_S[0] if last else 0.0
    if time.time() < _lane["next_at"]:
        return "gap"
    started = await session.scalar(select(func.count(SavedBatchItem.id)).where(
        SavedBatchItem.fed_at >= utc_now() - timedelta(hours=1)))
    if started >= BULK_MAX_PER_HOUR:
        return "hourly"
    if not within_hours(BULK_HOURS, datetime.now().hour):
        return "hours"
    return None


async def _feed_one(enqueue) -> str:
    async with async_session() as session:
        if not await _waiting_exists(session):
            return "idle"
        if blocker := await _pace_blocker(session):
            return blocker
        blockers = await route_blockers()
        conditions = _feedable(blockers)
        _lane["blocked"] = None
        for _ in range(SKIPS_PER_RUN):
            item = await _next_item(session, conditions) if conditions else None
            if item is None:
                if not await _waiting_exists(session):
                    return "idle"
                _lane["blocked"] = blockers["qobuz"] or blockers["youtube"]
                return "blocked"
            as_mp3 = item.route == "qobuz" and blockers["qobuz"] is not None
            track_id = await saved_download.feed_item(session, item, as_mp3=as_mp3)
            if track_id is not None:
                await enqueue(track_id, priority=BULK_PRIORITY)
                return "fed"
    return "skipped"


async def feed(enqueue) -> str:
    """One run of the lane: settle finished songs, read one album a batch reached, then start the next song
    when pacing and the daily shares allow. Returns what it did (fed, busy, gap, hourly, hours, blocked, idle)."""
    async with _feed_lock:
        await reconcile()
        await expand_next()
        status = await _feed_one(enqueue)
        _lane["status"] = status
        return status


# --- controls and summaries ---


class BatchRefused(Exception):
    """The batch cannot do that now (str() says why)."""


async def _batch(session, batch_id: int) -> SavedBatch:
    batch = await session.get(SavedBatch, batch_id)
    if batch is None:
        raise LookupError("This Download all no longer exists.")
    return batch


async def _resume_state(session, batch_id: int) -> str:
    pending = await session.scalar(select(SavedBatchItem.id).where(SavedBatchItem.batch_id == batch_id,
                                                                    SavedBatchItem.state == "expand").limit(1))
    return "expanding" if pending else "active"


async def pause(session, batch_id: int) -> None:
    batch = await _batch(session, batch_id)
    if batch.state not in FEEDING_STATES:
        raise BatchRefused("Only a running Download all can be paused.")
    batch.state = "paused"
    await session.commit()


async def resume(session, batch_id: int) -> None:
    batch = await _batch(session, batch_id)
    if batch.state != "paused":
        raise BatchRefused("Only a paused Download all can be resumed.")
    batch.state = await _resume_state(session, batch_id)
    await session.commit()


async def cancel(session, batch_id: int) -> None:
    """Nothing is deleted: waiting songs are cancelled, a song already handed over finishes normally."""
    batch = await _batch(session, batch_id)
    if batch.state in ("done", "cancelled"):
        raise BatchRefused("This Download all has already finished.")
    batch.state, batch.finished_at = "cancelled", utc_now()
    for item in (await session.scalars(select(SavedBatchItem).where(
            SavedBatchItem.batch_id == batch_id, SavedBatchItem.state.in_(("waiting", "expand"))))).all():
        item.state = "cancelled"
    await session.commit()


async def retry_failed(session, batch_id: int, item_id: int | None = None) -> int:
    """Failed songs (or one of them) wait again; returns how many."""
    batch = await _batch(session, batch_id)
    if batch.state == "cancelled":
        raise BatchRefused("This Download all was cancelled.")
    stmt = select(SavedBatchItem).where(SavedBatchItem.batch_id == batch_id, SavedBatchItem.state == "error")
    if item_id is not None:
        stmt = stmt.where(SavedBatchItem.id == item_id)
    items = (await session.scalars(stmt)).all()
    for item in items:
        item.state = "expand" if item.kind == "collection" else "waiting"
        item.note, item.finished_at, item.fed_at = None, None, None
        _expand_wait.pop(item.id, None)
    if items and batch.state == "done":
        batch.state, batch.finished_at = await _resume_state(session, batch_id), None
    await session.commit()
    return len(items)


def _title(item: SavedBatchItem) -> tuple[str, str]:
    try:
        data = json.loads(item.data or "{}").get("data", {})
    except ValueError:
        data = {}
    return data.get("title") or "", data.get("artist") or ""


async def _failures(session, batch_id: int) -> dict:
    failed = (await session.scalars(select(SavedBatchItem).where(SavedBatchItem.batch_id == batch_id,
                                                                 SavedBatchItem.state == "error")
                                    .order_by(SavedBatchItem.position, SavedBatchItem.id))).all()
    by_reason = Counter(item.note or "Failed" for item in failed)
    items = []
    for item in failed[:FAILED_LISTED]:
        title, artist = _title(item)
        items.append({"id": item.id, "title": title or ("An album" if item.kind == "collection" else ""),
                      "artist": artist, "note": item.note, "track_id": item.track_id})
    return {"count": len(failed), "by_reason": [{"reason": r, "count": n} for r, n in by_reason.most_common()],
            "items": items}


def _lane_view() -> dict:
    next_at = _lane["next_at"]
    left = max(0.0, next_at - time.time()) if next_at else 0.0
    return {"status": _lane["status"], "blocked": _lane["blocked"], "next_in_s": round(left) if left else 0,
            "per_hour": BULK_MAX_PER_HOUR, "hours": BULK_HOURS}


async def batch_summaries(session, *, with_failures: bool = False) -> list[dict]:
    """The open batches (running, paused) with their progress, the lane's next start and days left."""
    batches = (await session.scalars(select(SavedBatch).where(SavedBatch.state.in_(saved_download.OPEN_BATCH_STATES))
                                     .order_by(SavedBatch.id))).all()
    if not batches:
        return []
    counts: dict[int, Counter] = {b.id: Counter() for b in batches}
    estimated: dict[int, int] = Counter()
    albums: dict[int, int] = Counter()
    for batch_id, state, kind, n in await session.execute(
            select(SavedBatchItem.batch_id, SavedBatchItem.state, SavedBatchItem.kind, func.count(SavedBatchItem.id))
            .where(SavedBatchItem.batch_id.in_(counts)).group_by(SavedBatchItem.batch_id, SavedBatchItem.state,
                                                                  SavedBatchItem.kind)):
        if kind == "track":
            counts[batch_id][state] += n
        elif state == "expand":
            albums[batch_id] += n
    for batch_id, track_count in await session.execute(
            select(SavedBatchItem.batch_id, func.sum(SavedItem.track_count)).join(SavedItem)
            .where(SavedBatchItem.batch_id.in_(counts), SavedBatchItem.state == "expand").group_by(SavedBatchItem.batch_id)):
        estimated[batch_id] = int(track_count or 0)
    lane = _lane_view()
    summaries = []
    for batch in batches:
        c = counts[batch.id]
        skipped = sum(n for state, n in c.items() if state.startswith("skipped_") or state == "not_streamable")
        waiting = c["waiting"] + estimated[batch.id]
        per_day, left_today = await asyncio.to_thread(
            saved_download.daily_room, saved_download.lane_route(resolve_format(batch.format)[0]))
        summary = {
            "id": batch.id, "scope": batch.scope_text, "format": batch.format, "state": batch.state,
            "created_at": batch.created_at.replace(tzinfo=UTC).isoformat() if batch.created_at else None,
            "total": sum(c.values()) - c["cancelled"] + estimated[batch.id], "done": c["done"], "skipped": skipped,
            "failed": c["error"], "in_flight": c["fed"], "waiting": waiting, "albums_to_read": albums[batch.id],
            "days_left_at_least": saved_download.days_at_least(waiting, per_day, left_today) if waiting else 0,
            "next_in_s": lane["next_in_s"] if batch.state != "paused" else None, "lane": lane,
        }
        if with_failures:
            summary["failures"] = await _failures(session, batch.id)
        summaries.append(summary)
    return summaries
