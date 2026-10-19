"""Reads Qobuz favorites (tracks, albums, artists) and the user's playlists into the Saved snapshot. Every call
goes through the shared Qobuz throttle plus a 6 s gap of its own, so downloads keep most of the budget."""

import asyncio
import json
import logging
from datetime import timedelta

from sqlalchemy import or_, select

from config import settings
from models import utc_now
from models_saved import SavedItem
from services import qobuz_client, qobuz_engine, qobuz_session
from services.qobuz_client import QobuzAlbum, QobuzArtist, QobuzPlaylist, QobuzTrack
from services.saved_store import (
    SavedData,
    Stopped,
    active_ids,
    epoch_to_utc,
    replace_entries,
    run_step,
    seen_since,
    set_state,
    shift_positions,
    sweep,
    sync_state,
    unseen_since,
    upsert_items,
)

logger = logging.getLogger(__name__)

SOURCE = "qobuz"
SYNC_GAP_S = 6.0
FULL_EVERY = timedelta(days=7)
FEW_NEW = 10
FAVORITES_CHECK_NAME = "qobuz_favorites_check.json"
KIND_OF = {"tracks": "track", "albums": "album", "artists": "artist"}
KINDS = ("track", "album", "artist", "playlist")


def track_data(t: QobuzTrack) -> SavedData:
    return SavedData(
        source_id=t.id, title=t.full_title, artist=t.performer or None, album=t.album_title, album_source_id=t.album_id,
        album_artist=t.album_artist, image_url=t.cover_url, duration_ms=t.duration_s * 1000 if t.duration_s else None,
        track_number=t.track_number, disc_number=t.media_number, isrc=t.isrc, readable=t.streamable is not False,
        qobuz_max_bits=t.max_bit_depth, qobuz_max_khz=t.max_sampling_rate, qobuz_streamable=t.streamable,
        added_at=epoch_to_utc(t.favorited_at))


def album_data(a: QobuzAlbum) -> SavedData:
    title = f"{a.title} ({a.version})" if a.version and a.version.casefold() not in a.title.casefold() else a.title
    return SavedData(
        source_id=a.id, title=title, artist=a.artist or None, album_artist=a.artist or None, image_url=a.cover_url,
        year=a.year, total_tracks=a.tracks_count, total_discs=a.media_count, track_count=a.tracks_count,
        readable=a.streamable is not False, qobuz_max_bits=a.max_bit_depth, qobuz_max_khz=a.max_sampling_rate,
        qobuz_streamable=a.streamable, added_at=epoch_to_utc(a.favorited_at))


def artist_data(a: QobuzArtist) -> SavedData:
    return SavedData(source_id=a.id, title=a.name, image_url=a.image_url, added_at=epoch_to_utc(a.favorited_at))


def playlist_data(p: QobuzPlaylist, user_id: str) -> SavedData:
    return SavedData(source_id=p.id, title=p.name, artist=p.owner_name, track_count=p.tracks_count,
                     owner_is_me=p.owner_id == user_id if p.owner_id else None,
                     snapshot_tag=f"{p.updated_at}:{p.tracks_count}", readable=None)


_PARSE = {"tracks": track_data, "albums": album_data, "artists": artist_data}


def favorites_check() -> dict:
    """The per-kind verdict of `python -m scripts.qobuz_check --favorites`; {} when it never ran."""
    try:
        data = json.loads((settings.data_dir / FAVORITES_CHECK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class QobuzReader:
    def __init__(self, session_factory, client, fingerprint: str, *, full: bool,
                 gap: qobuz_client.Throttle | None = None) -> None:
        self.factory, self.client, self.fingerprint, self.full = session_factory, client, fingerprint, full
        self.gap = gap or qobuz_client.Throttle(60.0 / SYNC_GAP_S)
        self._ids: dict[str, list[str]] | None = None

    async def call(self, fn):
        def run():
            self.gap.wait()
            return fn()

        try:
            return await asyncio.to_thread(run)
        except qobuz_client.QobuzAuthError:
            qobuz_session.mark_expired(qobuz_session.EXPIRED_MESSAGE, self.fingerprint)
            raise Stopped("signed_out", state="blocked") from None
        except qobuz_session.SessionChanged:
            raise Stopped("session_changed", state="idle") from None
        except qobuz_client.QobuzRateLimitError as e:
            seconds = e.retry_after or qobuz_engine.COOLDOWN_DEFAULT_S
            qobuz_engine.set_cooldown(seconds)
            raise Stopped("rate_limited", resume_after=utc_now() + timedelta(seconds=seconds)) from None
        except (qobuz_client.QobuzShapeError, qobuz_client.QobuzAppError):
            raise Stopped("shape", state="off") from None

    async def list_call(self, fn):
        """A favorites or playlists list call: a 403 there is about the account, so all Qobuz work pauses."""
        try:
            return await self.call(fn)
        except qobuz_client.QobuzForbiddenError:
            await asyncio.to_thread(qobuz_engine._open_breaker, "unavailable")
            raise Stopped("forbidden", resume_after=utc_now() + timedelta(seconds=qobuz_engine.BREAKER_OPEN_S)) from None

    async def _favorite_ids(self) -> dict[str, list[str]]:
        if self._ids is None:
            try:
                self._ids = await self.list_call(self.client.favorite_ids)
            except Stopped as stop:
                if stop.reason != "shape":
                    raise
                self._ids = {}
        return self._ids

    async def favorites(self, qkind: str) -> None:
        kind = KIND_OF[qkind]
        head = await self.list_call(lambda: self.client.favorites(qkind, limit=1))
        ids = (await self._favorite_ids()).get(qkind)
        trusted = ids is not None and head.total is not None and len(ids) == head.total
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, kind)
            stored = await active_ids(session, SOURCE, kind)
            full = (self.full or state.full_requested or state.last_full_at is None
                    or utc_now() - state.last_full_at > FULL_EVERY)
        new = [i for i in ids if i not in stored] if trusted else []
        if trusted and not full and len(new) <= FEW_NEW and (qkind != "artists" or not new):
            await self._add_new(qkind, new)
            await self._finish(kind, removed={stored[sid] for sid in set(stored) - set(ids)}, full=False,
                               total=head.total)
        else:
            await self._full_pass(qkind, ids if trusted else None)

    async def _add_new(self, qkind: str, new: list[str]) -> None:
        """Up to FEW_NEW new favorites, fetched one by one so the answer's order does not matter."""
        fetched: list[tuple[SavedData, list[SavedData] | None]] = []
        for qobuz_id in new:
            try:
                if qkind == "tracks":
                    fetched.append((track_data(await self.call(lambda i=qobuz_id: self.client.get_track(i))), None))
                else:
                    album, tracks = await self.call(lambda i=qobuz_id: self.client.get_album(i))
                    fetched.append((album_data(album), [track_data(t) for t in tracks]))
            except qobuz_client.QobuzNotFoundError:
                logger.info("A new Qobuz favorite is gone from the catalogue; skipped")
        if fetched:
            await self._store(KIND_OF[qkind], fetched, first_position=0, shift=True)

    async def _store(self, kind: str, parsed: list, *, first_position: int, shift: bool = False) -> None:
        now = utc_now()
        async with self.factory() as session:
            if shift:
                await shift_positions(session, SOURCE, kind, len(parsed))
            await upsert_items(session, SOURCE, kind, [data for data, _ in parsed], now=now, first_position=first_position)
            for data, entries in parsed:
                if entries is not None:
                    item = await session.scalar(select(SavedItem).where(
                        SavedItem.source == SOURCE, SavedItem.kind == kind, SavedItem.source_id == data.source_id))
                    await replace_entries(session, item, entries, tag=None, now=now)
            await session.commit()

    async def _full_pass(self, qkind: str, ids: list[str] | None) -> None:
        """Every page, newest first; with trusted ids those decide removals, otherwise the pass must add up."""
        kind, started = KIND_OF[qkind], utc_now()
        pages = self.client.iter_favorites(qkind)
        offset = 0
        while (page := await self.list_call(lambda: next(pages, None))) is not None:
            await self._store(kind, [(_PARSE[qkind](item), None) for item in page.items], first_position=offset)
            offset += page.raw_count
            await set_state(self.factory, SOURCE, kind, done=offset, total_remote=page.total,
                            phase_text=f"{offset} / {page.total}" if page.total else None)
        async with self.factory() as session:
            if ids is not None:
                removed = {i for sid, i in (await active_ids(session, SOURCE, kind)).items() if sid not in set(ids)}
                total = len(ids)
            else:
                total = (await self.list_call(lambda: self.client.favorites(qkind, limit=1))).total
                complete = total is not None and await seen_since(session, SOURCE, kind, started) == total
                removed = set((await unseen_since(session, SOURCE, kind, started)).values()) if complete else set()
        await self._finish(kind, removed=removed, full=True, total=total)

    async def _finish(self, kind: str, *, removed: set[int], full: bool, total: int | None) -> None:
        now = utc_now()
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, kind)
            await sweep(session, state, removed, now=now)
            if full:
                state.last_full_at, state.full_requested = now, False
            else:
                state.last_incremental_at = now
            state.total_remote = total
            state.done = len(await active_ids(session, SOURCE, kind))
            await session.commit()

    async def playlists(self) -> None:
        started = utc_now()
        pages = self.client.iter_user_playlists()
        offset, total = 0, None
        while (page := await self.list_call(lambda: next(pages, None))) is not None:
            items = [playlist_data(p, self.client.user_id) for p in page.items]
            await self._store("playlist", [(data, None) for data in items], first_position=offset)
            offset += page.raw_count
            total = page.total if page.total is not None else total
        async with self.factory() as session:
            complete = total is not None and await seen_since(session, SOURCE, "playlist", started) == total
            removed = set((await unseen_since(session, SOURCE, "playlist", started)).values()) if complete else set()
        await self._finish("playlist", removed=removed, full=True, total=total)
        await self._playlist_entries()

    async def _playlist_entries(self) -> None:
        async with self.factory() as session:
            due = (await session.scalars(select(SavedItem).where(
                SavedItem.source == SOURCE, SavedItem.kind == "playlist", SavedItem.removed_at.is_(None),
                or_(SavedItem.entries_tag.is_(None), SavedItem.entries_tag != SavedItem.snapshot_tag),
                *([] if self.full else [SavedItem.readable.is_(True)])).order_by(SavedItem.position))).all()
        for item in due:
            readable, tracks = await self._playlist_tracks(item.source_id)
            async with self.factory() as session:
                row = await session.get(SavedItem, item.id)
                row.readable = readable
                if readable:
                    await replace_entries(session, row, [track_data(t) for t in tracks], tag=row.snapshot_tag,
                                          now=utc_now())
                await session.commit()

    async def _playlist_tracks(self, playlist_id: str) -> tuple[bool, list[QobuzTrack]]:
        """(readable, tracks); a 403 or 404 on one playlist only marks that playlist unreadable."""
        pages = self.client.iter_playlist_tracks(playlist_id)
        tracks: list[QobuzTrack] = []
        try:
            while (page := await self.call(lambda: next(pages, None))) is not None:
                tracks += page.items
        except (qobuz_client.QobuzForbiddenError, qobuz_client.QobuzNotFoundError):
            return False, []
        return True, tracks


async def _gate_closed(session_factory) -> bool:
    """Marks every kind and returns True when Qobuz must not be called now (cooldown or breaker)."""
    cooldown = qobuz_engine.cooldown_left()
    breaker = qobuz_engine.breaker_state()
    if not cooldown and not breaker["reason"]:
        return False
    resume = utc_now() + timedelta(seconds=cooldown or qobuz_engine.BREAKER_OPEN_S)
    for kind in KINDS:
        await set_state(session_factory, SOURCE, kind, state="paused", resume_after=resume,
                        blocked_reason="rate_limited" if cooldown else "forbidden", last_run_at=utc_now())
    return True


async def run(session_factory, *, full: bool = False, client=None, gap: qobuz_client.Throttle | None = None) -> None:
    """One refresh of every Qobuz kind the favorites check did not rule out."""
    stored = qobuz_session.usable()
    if stored is None:
        for kind in KINDS:
            await set_state(session_factory, SOURCE, kind, state="blocked", blocked_reason="signed_out",
                            last_run_at=utc_now())
        return
    if await _gate_closed(session_factory):
        return
    client = client or await asyncio.to_thread(qobuz_session.make_client, stored)
    fingerprint = qobuz_session.token_fingerprint(stored.user_auth_token)
    reader = QobuzReader(session_factory, client, fingerprint, full=full, gap=gap)
    check = favorites_check()
    steps = [(KIND_OF[q], f"{q}_ok", lambda q=q: reader.favorites(q)) for q in qobuz_client.FAVORITE_TYPES]
    steps.append(("playlist", "playlists_ok", reader.playlists))
    try:
        for kind, check_key, step in steps:
            if check.get(check_key) is False:
                await set_state(session_factory, SOURCE, kind, state="off", blocked_reason="check_failed",
                                last_run_at=utc_now())
                continue
            if not await run_step(session_factory, SOURCE, kind, step):
                return
    finally:
        if client.rotated_token:
            await asyncio.to_thread(qobuz_session.store_rotated_token, fingerprint, client.rotated_token)
        await asyncio.to_thread(client.close)
