"""Reads the owner's Spotify library into the Saved snapshot: liked songs, saved albums, playlists and (once the
follow permission is granted) followed artists, within a small daily share of Spotify calls."""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import spotipy
from sqlalchemy import or_, select

from models import utc_now
from models_saved import SavedItem
from services import spotify_client as sp
from services.saved_store import (
    SavedData,
    Stopped,
    active_ids,
    iso_to_utc,
    known_added,
    load_json,
    next_local_midnight,
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

SOURCE = "spotify"
DAILY_TOTAL = 300
DAILY_SYNC = 150
GAP_S, JITTER_S, BURST, BURST_WINDOW_S = 3.0, 1.5, 8, 30.0
FULL_EVERY = timedelta(days=14)
MISMATCH_FULL_MIN = timedelta(days=7)
FOLLOW_EVERY = timedelta(days=7)
PLAYLISTS_PER_RUN = 10
PAGE = 50
CONTAINS_BATCH = 50
_PAGE_CAP = {"track": sp.PAGE_CAPS["tracks"], "album": sp.PAGE_CAPS["albums"]}
_PAGE_FN = {"track": "saved_tracks_page", "album": "saved_albums_page"}


def track_data(t: dict) -> SavedData:
    return SavedData(
        source_id=t.get("id") or t["uri"], uri=t.get("uri") or None, title=t.get("name") or "",
        artist=t.get("artist") or None, album=t.get("album"), album_source_id=t.get("album_id"),
        album_artist=t.get("album_artist"), image_url=t.get("album_image_url"), duration_ms=t.get("duration_ms"),
        track_number=t.get("track_number"), disc_number=t.get("disc_number"), total_tracks=t.get("total_tracks"),
        year=t.get("year"), isrc=t.get("isrc"), readable=not t.get("is_local"), added_at=iso_to_utc(t.get("added_at")))


def album_data(a: dict) -> SavedData:
    return SavedData(
        source_id=a["id"], uri=a.get("uri") or None, title=a.get("name") or "", artist=a.get("artist") or None,
        album_artist=a.get("album_artist"), image_url=a.get("image_url"), year=a.get("year"),
        total_tracks=a.get("total_tracks"), track_count=a.get("total_tracks") or a.get("tracks_total"),
        added_at=iso_to_utc(a.get("added_at")))


def playlist_data(p: dict, me: str | None) -> SavedData:
    mine = bool(me) and p.get("owner_id") == me
    return SavedData(
        source_id=p["id"], uri=p.get("uri") or None, title=p.get("name") or "", artist=p.get("owner_name"),
        image_url=p.get("image_url"), track_count=p.get("track_count"), owner_is_me=mine or p.get("collaborative"),
        snapshot_tag=p.get("snapshot_id"), readable=None)


def artist_data(a: dict) -> SavedData:
    return SavedData(source_id=a["id"], uri=a.get("uri") or None, title=a.get("name") or "", image_url=a.get("image_url"))


def _unique(items: list[SavedData]) -> list[SavedData]:
    seen: set[str] = set()
    return [d for d in items if not (d.source_id in seen or seen.add(d.source_id))]


class SpotifyReader:
    def __init__(self, session_factory, client, *, full: bool, pacer: sp.SyncPacer | None = None) -> None:
        self.factory, self.client, self.full = session_factory, client, full
        self.pacer = pacer or sp.SyncPacer(GAP_S, JITTER_S, BURST, BURST_WINDOW_S)

    def _pace(self) -> None:
        """Before every library call (runs in the worker thread)."""
        if sp.is_rate_limited():
            raise Stopped("rate_limited", resume_after=utc_now() + timedelta(seconds=sp.rate_limit_seconds_left()))
        used = sp.calls_today()
        if used["sync"] >= DAILY_SYNC or used["total"] >= DAILY_TOTAL:
            raise Stopped("daily_budget", resume_after=next_local_midnight())
        self.pacer.wait()

    async def call(self, fn, *, paces_itself: bool = False):
        """fn(client) — or fn(client, pace) when it pages on its own — in a thread, counted as library reading."""
        def run():
            with sp.counting_as_sync():
                if paces_itself:
                    return fn(self.client, self._pace)
                self._pace()
                return fn(self.client)

        try:
            result = await asyncio.to_thread(run)
        except spotipy.SpotifyException as e:
            if e.http_status == 429 or sp.is_rate_limited():
                raise self._limited() from None
            raise
        if result is None:
            raise self._limited() if sp.is_rate_limited() else Stopped("not_connected", state="blocked")
        return result

    @staticmethod
    def _limited() -> Stopped:
        return Stopped("rate_limited", resume_after=utc_now() + timedelta(seconds=sp.rate_limit_seconds_left()))

    # --- liked songs and saved albums ---

    def _page_fn(self, kind: str):
        return getattr(sp, _PAGE_FN[kind])

    def _parse(self, kind: str, page: sp.SavedPage) -> list[tuple[SavedData, list[SavedData] | None]]:
        if kind == "track":
            return [(track_data(t), None) for t in page.items]
        return [(album_data(a), [track_data(t) for t in a["tracks"]] if a.get("tracks") is not None else None)
                for a in page.items]

    async def _store(self, session, kind: str, parsed, *, first_position: int, now: datetime) -> None:
        unique = {data.source_id: (data, entries) for data, entries in parsed}
        await upsert_items(session, SOURCE, kind, [data for data, _ in unique.values()], now=now,
                           first_position=first_position)
        for data, entries in unique.values():
            if entries is not None:
                item = await session.scalar(select(SavedItem).where(
                    SavedItem.source == SOURCE, SavedItem.kind == kind, SavedItem.source_id == data.source_id))
                await replace_entries(session, item, entries, tag=None, now=now)

    async def saved_list(self, kind: str) -> None:
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, kind)
            full = (self.full or state.full_requested or state.full_started_at is not None or state.last_full_at is None
                    or utc_now() - state.last_full_at > FULL_EVERY)
        await (self._full_pass(kind) if full else self._incremental(kind))

    async def _full_pass(self, kind: str) -> None:
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, kind)
            if state.full_started_at is None:
                state.full_started_at, state.cursor, state.full_requested = utc_now(), None, False
                state.placeholders = state.raw_seen = state.done = 0
            # {"offset": null} once every page is stored: a pause before the final check resumes there.
            started, offset = state.full_started_at, load_json(state.cursor).get("offset", 0)
            await session.commit()
        if offset is not None and not await self._read_pages(kind, offset):
            return
        await self._finish_full(kind, started)

    async def _read_pages(self, kind: str, offset: int) -> bool:
        """Stores pages from `offset`, saving the cursor after each; False when the page cap ends this run first."""
        page_fn = self._page_fn(kind)
        for _ in range(_PAGE_CAP[kind]):
            page = await self.call(lambda c, at=offset: page_fn(at, PAGE, client=c))
            async with self.factory() as session:
                await self._store(session, kind, self._parse(kind, page), first_position=offset, now=utc_now())
                state = await sync_state(session, SOURCE, kind)
                state.placeholders += page.placeholders
                state.raw_seen += page.raw_count
                state.total_remote = page.total
                state.done = offset + page.raw_count
                state.cursor = json.dumps({"offset": page.next})
                state.phase_text = f"{state.done} / {page.total}" if page.total else None
                await session.commit()
            if page.next is None:
                return True
            offset = page.next
        return False

    async def _finish_full(self, kind: str, started: datetime) -> None:
        """Sweep only after a pass whose distinct items plus placeholders equal Spotify's total, read again now."""
        head = await self.call(lambda c: self._page_fn(kind)(0, 1, client=c))
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, kind)
            complete = head.total is not None and await seen_since(session, SOURCE, kind, started) + state.placeholders == head.total
            unseen = await unseen_since(session, SOURCE, kind, started)
        still = await self._still_saved(kind, list(unseen)) if complete and unseen else set()
        now = utc_now()
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, kind)
            if complete:
                await sweep(session, state, {i for sid, i in unseen.items() if sid not in still}, now=now)
                for item in (await session.scalars(select(SavedItem).where(SavedItem.id.in_(
                        [unseen[sid] for sid in still])))).all():
                    item.last_seen_at = now
            elif unseen:
                logger.info("Spotify %s pass did not add up to Spotify's total; nothing removed", kind)
            state.full_started_at, state.cursor, state.last_full_at = None, None, now
            state.total_remote = head.total
            await session.commit()

    async def _still_saved(self, kind: str, source_ids: list[str]) -> set[str]:
        """The removal candidates Spotify says are still saved; empty when it will not answer (then the
        completeness and mass-removal rules decide alone)."""
        ids = [sid for sid in source_ids if not sid.startswith("spotify:local:")]
        still: set[str] = set()
        for start in range(0, len(ids), CONTAINS_BATCH):
            batch = ids[start:start + CONTAINS_BATCH]
            try:
                answer = await self.call(lambda c, b=batch: sp.saved_contains(kind, b, client=c))
            except Stopped as stop:
                if stop.reason != "not_connected":
                    raise
                return still
            still |= {sid for sid, saved in zip(batch, answer, strict=True) if saved}
        return still

    async def _incremental(self, kind: str) -> None:
        """Newest first by added_at: stops at the first item already stored with the same added_at."""
        fresh: list = []
        offset, placeholders, total = 0, 0, None
        page_fn = self._page_fn(kind)
        for _ in range(_PAGE_CAP[kind]):
            page = await self.call(lambda c, at=offset: page_fn(at, PAGE, client=c))
            total = page.total if isinstance(page.total, int) else total
            if not page.raw_count:
                break
            parsed = self._parse(kind, page)
            async with self.factory() as session:
                known = await known_added(session, SOURCE, kind, {data.source_id for data, _ in parsed})
            stop = next((i for i, (data, _) in enumerate(parsed)
                         if data.source_id in known and known[data.source_id] == data.added_at), None)
            fresh += parsed if stop is None else parsed[:stop]
            if stop is not None or page.next is None:
                break
            placeholders += page.placeholders
            offset = page.next
        await self._store_incremental(kind, fresh, placeholders, total)

    async def _store_incremental(self, kind: str, fresh: list, placeholders: int, total: int | None) -> None:
        now = utc_now()
        async with self.factory() as session:
            unique = list({data.source_id: (data, entries) for data, entries in fresh}.values())
            await shift_positions(session, SOURCE, kind, len(unique))
            await self._store(session, kind, unique, first_position=0, now=now)
            state = await sync_state(session, SOURCE, kind)
            state.placeholders += placeholders
            expected = len(await active_ids(session, SOURCE, kind)) + state.placeholders
            if total is not None and total != expected and (
                    state.last_mismatch_full_at is None or now - state.last_mismatch_full_at > MISMATCH_FULL_MIN):
                logger.info("Spotify %s: %s on Spotify, %s here; a full read is scheduled", kind, total, expected)
                state.full_requested, state.last_mismatch_full_at = True, now
            state.last_incremental_at, state.total_remote, state.done = now, total, expected
            await session.commit()

    # --- playlists ---

    async def _me(self) -> str | None:
        async with self.factory() as session:
            cached = load_json((await sync_state(session, SOURCE, "playlist")).cursor).get("me")
        if cached:
            return cached
        profile = await self.call(lambda c: c.current_user())
        me = profile.get("id") if isinstance(profile, dict) else None
        if me:
            await set_state(self.factory, SOURCE, "playlist", cursor=json.dumps({"me": me}))
        return me

    async def playlists(self) -> None:
        me = await self._me()
        started = utc_now()
        listing = await self.call(lambda c, pace: sp.fetch_user_playlists(pace=pace, client=c), paces_itself=True)
        if listing.stopped == "rate_limited":
            raise self._limited()
        items = _unique([playlist_data(p, me) for p in listing.items])
        async with self.factory() as session:
            await upsert_items(session, SOURCE, "playlist", items, now=utc_now())
            state = await sync_state(session, SOURCE, "playlist")
            state.total_remote, state.placeholders = listing.total, listing.placeholders
            if listing.complete and listing.total == len(items) + listing.placeholders:
                unseen = await unseen_since(session, SOURCE, "playlist", started)
                await sweep(session, state, set(unseen.values()), now=utc_now())
            await session.commit()
        await self._playlist_entries()

    async def _playlist_entries(self) -> None:
        """Items of up to PLAYLISTS_PER_RUN playlists whose snapshot changed; refused ones again only on a full run."""
        async with self.factory() as session:
            due = (await session.scalars(select(SavedItem).where(
                SavedItem.source == SOURCE, SavedItem.kind == "playlist", SavedItem.removed_at.is_(None),
                or_(SavedItem.entries_tag.is_(None), SavedItem.entries_tag != SavedItem.snapshot_tag),
                *([] if self.full else [SavedItem.readable.is_(True)]))
                .order_by(SavedItem.owner_is_me.desc(), SavedItem.position).limit(PLAYLISTS_PER_RUN))).all()
        for item in due:
            listing = await self.call(lambda c, pace, pid=item.source_id: sp.fetch_playlist_items(pid, pace=pace, client=c),
                                      paces_itself=True)
            if listing.stopped == "rate_limited":
                raise self._limited()
            async with self.factory() as session:
                row = await session.get(SavedItem, item.id)
                if listing.stopped == "unreadable":
                    row.readable = False
                else:
                    row.readable = True
                    await replace_entries(session, row, [track_data(t) for t in listing.items], tag=row.snapshot_tag,
                                          now=utc_now())
                await session.commit()

    # --- followed artists ---

    async def artists(self) -> None:
        async with self.factory() as session:
            state = await sync_state(session, SOURCE, "artist")
            probed_recently = state.scope_probe_at is not None and utc_now() - state.scope_probe_at < FOLLOW_EVERY
            verdict = load_json(state.cursor).get("probe")
            if probed_recently and verdict != "granted":
                raise Stopped(verdict or "scope_missing", state="blocked")
            if not self.full and state.last_full_at is not None and utc_now() - state.last_full_at < FOLLOW_EVERY:
                return
        verdict = await self.call(lambda c: sp.follow_scope_status(client=c))
        await set_state(self.factory, SOURCE, "artist", scope_probe_at=utc_now(), cursor=json.dumps({"probe": verdict}))
        if verdict != "granted":
            raise Stopped("scope_missing" if verdict == "scope_missing" else "forbidden", state="blocked")
        started = utc_now()
        listing = await self.call(lambda c, pace: sp.fetch_followed_artists(pace=pace, client=c), paces_itself=True)
        if listing.stopped == "rate_limited":
            raise self._limited()
        items = _unique([artist_data(a) for a in listing.items])
        async with self.factory() as session:
            await upsert_items(session, SOURCE, "artist", items, now=utc_now())
            state = await sync_state(session, SOURCE, "artist")
            if listing.complete and (listing.total is None or len(items) + listing.placeholders == listing.total):
                await sweep(session, state, set((await unseen_since(session, SOURCE, "artist", started)).values()),
                            now=utc_now())
            state.last_full_at, state.total_remote = utc_now(), listing.total
            await session.commit()


async def run(session_factory, *, full: bool = False, client=None, pacer: sp.SyncPacer | None = None) -> None:
    """One refresh of every Spotify kind. Followed artists are left out of the very first read."""
    client = client or await asyncio.to_thread(sp.get_client)
    if client is None:
        for kind in ("track", "album", "playlist", "artist"):
            await set_state(session_factory, SOURCE, kind, state="blocked", blocked_reason="not_connected",
                            last_run_at=utc_now())
        return
    reader = SpotifyReader(session_factory, client, full=full, pacer=pacer)
    async with session_factory() as session:
        first_read = (await sync_state(session, SOURCE, "track")).last_full_at is None
        await session.commit()
    steps = [("track", lambda: reader.saved_list("track")), ("album", lambda: reader.saved_list("album")),
             ("playlist", reader.playlists)]
    if not first_read:
        steps.append(("artist", reader.artists))
    for kind, step in steps:
        if not await run_step(session_factory, SOURCE, kind, step):
            return
