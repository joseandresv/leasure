"""Keeps the Saved snapshot current: one background task refreshes each source on its schedule (one job per
source at a time) and re-matches the snapshot against the library whenever either changes."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from db import async_session
from models import utc_now
from models_saved import SavedItem, SavedSyncState
from services import (
    qobuz_engine,
    qobuz_session,
    saved_bulk,
    saved_match,
    saved_qobuz,
    saved_spotify,
    saved_store,
    saved_youtube,
    youtube_client,
)
from services import spotify_client as sp

logger = logging.getLogger(__name__)

TICK_S = 600
SPOTIFY_INCREMENTAL_HOUR = 4
QOBUZ_EVERY = timedelta(hours=12)
YT_EVERY = timedelta(hours=12)
RUNNING_POLL_S = 2
PAUSED_POLL_S = 60
LABELS = {"spotify": "Spotify", "qobuz": "Qobuz", "youtube": "YT Music"}
KINDS = {"spotify": ("track", "album", "playlist", "artist"), "qobuz": saved_qobuz.KINDS,
         "youtube": tuple(saved_youtube.FETCHERS)}
_RUNNERS = {
    "spotify": lambda factory, full: saved_spotify.run(factory, full=full),
    "qobuz": lambda factory, full: saved_qobuz.run(factory, full=full),
    "youtube": lambda factory, full: saved_youtube.run(factory),
}


class RefreshRefused(Exception):
    def __init__(self, reason: str, resume_after: datetime | None = None) -> None:
        super().__init__(reason)
        self.reason, self.resume_after = reason, resume_after


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value else None


def _local(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC).astimezone() if value else None


def when_label(value: datetime | None, now: datetime | None = None) -> str | None:
    """"today 09:12", "yesterday 21:40" or "3 Oct 09:12" in local time."""
    moment = _local(value)
    if moment is None:
        return None
    today = (_local(now) if now else datetime.now().astimezone()).date()
    day = {today: "today", today - timedelta(days=1): "yesterday"}.get(moment.date(), f"{moment.day} {moment:%b}")
    return f"{day} {moment:%H:%M}"


async def _states(session, source: str) -> dict[str, SavedSyncState]:
    rows = (await session.scalars(select(SavedSyncState).where(SavedSyncState.source == source))).all()
    return {row.kind: row for row in rows}


def _latest(states, name: str) -> datetime | None:
    return max((getattr(s, name) for s in states if getattr(s, name)), default=None)


def spotify_never_read(states: dict[str, SavedSyncState]) -> bool:
    track = states.get("track")
    return track is None or (track.last_full_at is None and track.full_started_at is None)


def _due_after_pause(states) -> bool | None:
    """True/False while a paused kind decides, None when nothing is paused."""
    paused = [s for s in states if s.state == "paused"]
    if not paused:
        return None
    return all(s.resume_after is None or s.resume_after <= utc_now() for s in paused)


async def refresh_blocker(source: str) -> RefreshRefused | None:
    """Why a refresh of `source` cannot start right now, from the live gates rather than the stored states."""
    if source == "spotify":
        if sp.is_rate_limited():
            return RefreshRefused("rate_limited", utc_now() + timedelta(seconds=sp.rate_limit_seconds_left()))
        used = sp.calls_today()
        if used["sync"] >= saved_spotify.DAILY_SYNC or used["total"] >= saved_spotify.DAILY_TOTAL:
            return RefreshRefused("daily_budget", saved_store.next_local_midnight())
        if not await asyncio.to_thread(sp.is_connected):
            return RefreshRefused("not_connected")
    elif source == "qobuz":
        if qobuz_session.usable() is None:
            return RefreshRefused("signed_out")
        if seconds := qobuz_engine.cooldown_left():
            return RefreshRefused("rate_limited", utc_now() + timedelta(seconds=seconds))
        if qobuz_engine.breaker_state()["reason"]:
            return RefreshRefused("forbidden")
    elif not youtube_client.HEADERS_PATH.exists():
        return RefreshRefused("not_connected")
    return None


class SavedSync:
    def __init__(self, session_factory=None) -> None:
        self.factory = session_factory or async_session
        self._loop_task: asyncio.Task | None = None
        self._jobs: dict[str, asyncio.Task] = {}
        self._rematch_task: asyncio.Task | None = None
        self._rematch_again = False
        self._library_signature: tuple | None = None

    async def start(self) -> None:
        async with self.factory() as session:
            await session.execute(update(SavedSyncState).where(SavedSyncState.state == "running")
                                  .values(state="idle", phase_text=None))
            await session.commit()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in (self._loop_task, self._rematch_task, *self._jobs.values()) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task, self._rematch_task, self._jobs = None, None, {}

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Saved-library scheduler tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        for source in _RUNNERS:
            if not self.running(source) and await self.due(source):
                self.start_job(source, full=False)
        async with self.factory() as session:
            signature = await saved_match.library_signature(session)
            await saved_store.purge_removed(session)
            await session.commit()
        if signature != self._library_signature:
            self._library_signature = signature
            self.request_rematch()

    async def due(self, source: str) -> bool:
        async with self.factory() as session:
            states = await _states(session, source)
        paused = _due_after_pause(states.values())
        if paused is not None:
            return paused
        last_run = _latest(states.values(), "last_run_at")
        if source == "spotify":
            if spotify_never_read(states):
                return False
            now = datetime.now().astimezone()
            return now.hour >= SPOTIFY_INCREMENTAL_HOUR and (last_run is None or _local(last_run).date() < now.date())
        if source == "qobuz" and qobuz_session.usable() is None:
            return False
        if source == "youtube" and not youtube_client.HEADERS_PATH.exists():
            return False
        # Signed in again after a run found Qobuz signed out: read now rather than in 12 hours.
        if any(s.blocked_reason == "signed_out" for s in states.values()):
            return True
        return last_run is None or utc_now() - last_run >= (QOBUZ_EVERY if source == "qobuz" else YT_EVERY)

    def running(self, source: str) -> bool:
        task = self._jobs.get(source)
        return task is not None and not task.done()

    def start_job(self, source: str, *, full: bool) -> bool:
        if self.running(source):
            return False
        self._jobs[source] = asyncio.create_task(self._run_job(source, full))
        return True

    async def _run_job(self, source: str, full: bool) -> None:
        try:
            await _RUNNERS[source](self.factory, full)
        except Exception:
            logger.exception("Saved %s refresh failed", source)
        finally:
            self.request_rematch()

    async def request_refresh(self, source: str, *, full: bool) -> None:
        """Starts a refresh now; raises RefreshRefused when it is running already or a gate is closed."""
        if self.running(source):
            raise RefreshRefused("running")
        if refused := await refresh_blocker(source):
            raise refused
        if full:
            async with self.factory() as session:
                for kind in KINDS[source]:
                    (await saved_store.sync_state(session, source, kind)).full_requested = True
                await session.commit()
        self.start_job(source, full=full)

    def request_rematch(self) -> None:
        if self._rematch_task is not None and not self._rematch_task.done():
            self._rematch_again = True
            return
        self._rematch_task = asyncio.create_task(self._rematch())

    async def _rematch(self) -> None:
        while True:
            self._rematch_again = False
            try:
                await saved_match.rematch_all(self.factory)
            except Exception:
                logger.exception("Saved-library rematch failed")
            if not self._rematch_again:
                return

    async def forget_scope_probe(self) -> None:
        """After a Spotify reconnect the follow permission may be granted now: probe again on the next run."""
        await saved_store.set_state(self.factory, "spotify", "artist", scope_probe_at=None, cursor=None,
                                    blocked_reason=None, state="idle")

    async def status(self) -> dict:
        async with self.factory() as session:
            counts: dict[tuple[str, str], int] = {(source, kind): n for source, kind, n in await session.execute(
                select(SavedItem.source, SavedItem.kind, func.count(SavedItem.id))
                .where(SavedItem.removed_at.is_(None)).group_by(SavedItem.source, SavedItem.kind))}
            states = {source: await _states(session, source) for source in _RUNNERS}
            batches = await saved_bulk.batch_summaries(session)
        sources = [await self._source_status(source, states[source], counts) for source in _RUNNERS]
        if any(s["running"] for s in sources):
            poll = RUNNING_POLL_S
        elif any(s["state"] in ("paused", "blocked") and s["resume_after"] for s in sources) or \
                any(b["state"] in saved_bulk.FEEDING_STATES for b in batches):
            poll = PAUSED_POLL_S
        else:
            poll = None
        return {"sources": sources, "any_running": poll == RUNNING_POLL_S, "poll_s": poll, "batches": batches}

    async def _source_status(self, source: str, states: dict[str, SavedSyncState], counts: dict) -> dict:
        connected = await self._connected(source)
        running = self.running(source)
        kinds = [_kind_status(kind, states.get(kind), counts.get((source, kind), 0)) for kind in KINDS[source]]
        never = spotify_never_read(states) if source == "spotify" else not any(s.last_ok_at for s in states.values())
        resume = _latest([s for s in states.values() if s.state == "paused"], "resume_after")
        reasons = [k["blocked_reason"] for k in kinds if k["blocked_reason"]]
        state = _source_state(running=running, connected=connected, never=never, kinds=kinds)
        status = {
            "source": source, "label": LABELS[source], "connected": connected, "running": running, "state": state,
            "never_read": never, "blocked_reason": reasons[0] if reasons else None,
            "resume_after": _iso(resume), "resume_label": when_label(resume),
            "phase_text": next((k["phase_text"] for k in kinds if k["state"] == "running" and k["phase_text"]), None),
            "refreshed_at": _iso(_latest(states.values(), "last_ok_at")),
            "refreshed_label": when_label(_latest(states.values(), "last_ok_at")),
            "counts": {k["kind"]: k["count"] for k in kinds}, "kinds": kinds,
            "scope_missing": any(k["kind"] == "artist" and k["blocked_reason"] == "scope_missing" for k in kinds),
            "kinds_off": [k["kind"] for k in kinds if k["state"] == "off"],
        }
        if source == "spotify":
            status["calls_today"] = sp.calls_today()
            status["daily_sync_budget"] = saved_spotify.DAILY_SYNC
        if source == "qobuz":
            stored = qobuz_session.load()
            status["expired"] = stored is not None and stored.state == "expired"
        return status

    async def _connected(self, source: str) -> bool:
        if source == "spotify":
            return await asyncio.to_thread(sp.is_connected)
        if source == "qobuz":
            return qobuz_session.usable() is not None
        return youtube_client.HEADERS_PATH.exists()


def _kind_status(kind: str, state: SavedSyncState | None, count: int) -> dict:
    if state is None:
        return {"kind": kind, "state": "idle", "blocked_reason": None, "phase_text": None, "count": count,
                "done": 0, "total_remote": None, "last_ok_at": None, "last_error": None, "resume_after": None}
    return {"kind": kind, "state": state.state, "blocked_reason": state.blocked_reason, "phase_text": state.phase_text,
            "count": count, "done": state.done, "total_remote": state.total_remote,
            "last_ok_at": _iso(state.last_ok_at), "last_error": state.last_error,
            "resume_after": _iso(state.resume_after)}


def _source_state(*, running: bool, connected: bool, never: bool, kinds: list[dict]) -> str:
    """running | not_connected | never | paused | blocked | error | fresh"""
    if running:
        return "running"
    if not connected:
        return "not_connected"
    if never:
        return "never"
    # A missing follow permission only affects Artists; the strip says so on its own line.
    states = {k["state"] for k in kinds if k["blocked_reason"] != "scope_missing"}
    for state in ("paused", "blocked", "error"):
        if state in states:
            return state
    return "fresh"


saved_sync = SavedSync()
