"""The Saved snapshot: what the refresh jobs write, and the removal sweep that never drops a list on a
short or failed answer. DB only, no network."""

import json
import logging
import unicodedata
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import utc_now
from models_saved import SavedEntry, SavedItem, SavedSyncState
from services import library_match, local_albums

logger = logging.getLogger(__name__)

SOURCES = ("spotify", "qobuz", "youtube")
KINDS = ("track", "album", "playlist", "artist")
SWEEP_MAX_FRACTION = 0.10
PURGE_AFTER_D = 90
GROUP_KEY_MAX = 200


@dataclass
class SavedData:
    """One saved item or collection entry as a service described it."""
    source_id: str
    title: str
    artist: str | None = None
    album: str | None = None
    album_source_id: str | None = None
    album_artist: str | None = None
    uri: str | None = None
    image_url: str | None = None
    duration_ms: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    total_tracks: int | None = None
    total_discs: int | None = None
    year: int | None = None
    isrc: str | None = None
    readable: bool | None = True       # None: keep what is stored (a playlist list does not know)
    qobuz_max_bits: int | None = None
    qobuz_max_khz: float | None = None
    qobuz_streamable: bool | None = None
    added_at: datetime | None = None
    track_count: int | None = None
    owner_is_me: bool | None = None
    snapshot_tag: str | None = None


_ITEM_ONLY = ("track_count", "owner_is_me", "snapshot_tag")
_DESCRIBED = tuple(f.name for f in fields(SavedData) if f.name not in _ITEM_ONLY)


def plain_text(text: str | None) -> str:
    """Casefolded, accent-free text for the filter box: "Beyoncé" matches "beyonce"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).casefold().split())


def group_key(source: str, kind: str, data: SavedData) -> str:
    """Songs and albums saved on several services share a key; playlists never merge."""
    key = None
    if kind == "track" and data.title:
        key = library_match.song_group_key(data.title, data.artist)
    elif kind == "album" and (album := local_albums.album_key(data.title, data.artist, data.artist)):
        key = f"album:{album.key}"
    elif kind == "artist" and data.title:
        key = f"artist:{local_albums.norm_v2(data.title)}"
    return (key or f"{kind}:{source}:{data.source_id}")[:GROUP_KEY_MAX]


def epoch_to_utc(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def iso_to_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


def https_only(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _describe(row, source: str, kind: str, data: SavedData) -> None:
    for name in _DESCRIBED:
        value = getattr(data, name)
        if name in ("added_at", "readable") and value is None:
            continue
        setattr(row, name, value)
    row.image_url = https_only(data.image_url)
    row.group_key = group_key(source, kind, data)


async def sync_state(session: AsyncSession, source: str, kind: str) -> SavedSyncState:
    state = await session.scalar(select(SavedSyncState).where(SavedSyncState.source == source,
                                                              SavedSyncState.kind == kind))
    if state is None:
        state = SavedSyncState(source=source, kind=kind, state="idle", done=0, raw_seen=0, placeholders=0,
                               full_requested=False)
        session.add(state)
        await session.flush()
    return state


async def upsert_items(session: AsyncSession, source: str, kind: str, items: list[SavedData], *,
                       now: datetime, first_position: int = 0) -> int:
    """Insert or refresh `items` as seen at `now`, in service order from `first_position`; a removed item that
    reappears comes back. Returns how many were new."""
    if not items:
        return 0
    existing = {row.source_id: row for row in (await session.scalars(select(SavedItem).where(
        SavedItem.source == source, SavedItem.kind == kind,
        SavedItem.source_id.in_({d.source_id for d in items})))).all()}
    new = 0
    for offset, data in enumerate(items):
        row = existing.get(data.source_id)
        if row is None:
            row = SavedItem(source=source, kind=kind, first_seen_at=now)
            session.add(row)
            existing[data.source_id] = row
            new += 1
        _describe(row, source, kind, data)
        row.track_count = data.track_count if data.track_count is not None else row.track_count
        row.owner_is_me = data.owner_is_me
        row.snapshot_tag = data.snapshot_tag
        row.search_text = plain_text(f"{data.title} {data.artist or ''} {data.album or ''}")
        row.position = first_position + offset
        row.last_seen_at = now
        row.removed_at = None
    await session.flush()
    return new


async def shift_positions(session: AsyncSession, source: str, kind: str, by: int) -> None:
    """Make room at the top of a newest-first list for `by` new items."""
    if by:
        await session.execute(update(SavedItem).where(SavedItem.source == source, SavedItem.kind == kind)
                              .values(position=SavedItem.position + by))


async def replace_entries(session: AsyncSession, item: SavedItem, entries: list[SavedData], *, tag: str | None,
                          now: datetime) -> None:
    await session.execute(delete(SavedEntry).where(SavedEntry.collection_id == item.id))
    for position, data in enumerate(entries):
        entry = SavedEntry(collection_id=item.id, position=position)
        _describe(entry, item.source, "track", data)
        session.add(entry)
    item.entries_tag, item.entries_fetched_at, item.entries_matched = tag, now, None
    if item.track_count is None:
        item.track_count = len(entries)
    await session.flush()


async def active_ids(session: AsyncSession, source: str, kind: str) -> dict[str, int]:
    """source_id -> row id of every item still saved."""
    rows = await session.execute(select(SavedItem.source_id, SavedItem.id).where(
        SavedItem.source == source, SavedItem.kind == kind, SavedItem.removed_at.is_(None)))
    return {source_id: item_id for source_id, item_id in rows}


async def known_added(session: AsyncSession, source: str, kind: str, source_ids: set[str]) -> dict[str, datetime | None]:
    rows = await session.execute(select(SavedItem.source_id, SavedItem.added_at).where(
        SavedItem.source == source, SavedItem.kind == kind, SavedItem.removed_at.is_(None),
        SavedItem.source_id.in_(source_ids)))
    return dict(rows.all())


async def seen_since(session: AsyncSession, source: str, kind: str, since: datetime) -> int:
    return await session.scalar(select(func.count(SavedItem.id)).where(
        SavedItem.source == source, SavedItem.kind == kind, SavedItem.removed_at.is_(None),
        SavedItem.last_seen_at >= since)) or 0


async def unseen_since(session: AsyncSession, source: str, kind: str, since: datetime) -> dict[str, int]:
    """Items still saved that a pass started at `since` did not see: the removal candidates."""
    rows = await session.execute(select(SavedItem.source_id, SavedItem.id).where(
        SavedItem.source == source, SavedItem.kind == kind, SavedItem.removed_at.is_(None),
        SavedItem.last_seen_at < since))
    return {source_id: item_id for source_id, item_id in rows}


@dataclass(frozen=True)
class SweepOutcome:
    removed: int = 0
    deferred: int = 0


async def sweep(session: AsyncSession, state: SavedSyncState, candidates: set[int], *, now: datetime,
                always_two_pass: bool = False) -> SweepOutcome:
    """Hide (never delete) `candidates`. A mass removal (over SWEEP_MAX_FRACTION, or everything), or any
    removal when `always_two_pass`, waits until the next full pass finds the same items missing again."""
    if not candidates:
        state.pending_removals = None
        return SweepOutcome()
    active = await session.scalar(select(func.count(SavedItem.id)).where(
        SavedItem.source == state.source, SavedItem.kind == state.kind, SavedItem.removed_at.is_(None))) or 0
    remove = set(candidates)
    if always_two_pass or len(candidates) > SWEEP_MAX_FRACTION * active or len(candidates) >= active:
        previous = set(json.loads(state.pending_removals or "[]"))
        remove = candidates & previous
    waiting = candidates - remove
    state.pending_removals = json.dumps(sorted(waiting)) if waiting else None
    if remove:
        await session.execute(update(SavedItem).where(SavedItem.id.in_(remove)).values(removed_at=now))
    return SweepOutcome(removed=len(remove), deferred=len(waiting))


async def purge_removed(session: AsyncSession, now: datetime | None = None) -> int:
    cutoff = (now or utc_now()) - timedelta(days=PURGE_AFTER_D)
    result = await session.execute(delete(SavedItem).where(SavedItem.removed_at.is_not(None),
                                                           SavedItem.removed_at < cutoff))
    return result.rowcount or 0


def load_json(text: str | None) -> dict:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class Stopped(Exception):
    """A refresh step that ends early: `state` paused (resumes after `resume_after`), blocked, error or off."""

    def __init__(self, reason: str, *, state: str = "paused", resume_after: datetime | None = None,
                 message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason, self.state, self.resume_after = reason, state, resume_after


def next_local_midnight() -> datetime:
    """Tomorrow 00:00 local time, as naive UTC."""
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(UTC).replace(tzinfo=None)


async def set_state(session_factory, source: str, kind: str, **values) -> None:
    async with session_factory() as session:
        state = await sync_state(session, source, kind)
        for name, value in values.items():
            setattr(state, name, value)
        await session.commit()


# A source stops reading for the rest of the run on these; other kinds' failures only stop that kind.
HALTING = ("rate_limited", "daily_budget", "not_connected", "signed_out", "forbidden", "session_changed")


async def run_step(session_factory, source: str, kind: str, step) -> bool:
    """Runs one kind's refresh and records the outcome; False when the whole source has to stop."""
    await set_state(session_factory, source, kind, state="running", blocked_reason=None, resume_after=None,
                    phase_text=None)
    try:
        await step()
    except Stopped as stop:
        logger.info("Saved %s %s stopped: %s", source, kind, stop.reason)
        await set_state(session_factory, source, kind, state=stop.state, blocked_reason=stop.reason,
                        resume_after=stop.resume_after, phase_text=None, last_run_at=utc_now())
        return stop.reason not in HALTING
    except Exception as e:
        status = getattr(e, "http_status", None) or getattr(e, "status", None)
        error = f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")
        logger.warning("Saved %s %s failed: %s", source, kind, error, exc_info=True)
        await set_state(session_factory, source, kind, state="error", last_error=error[:200], phase_text=None,
                        last_run_at=utc_now())
        return True
    await set_state(session_factory, source, kind, state="idle", last_ok_at=utc_now(), last_run_at=utc_now(),
                    last_error=None, blocked_reason=None, phase_text=None)
    return True
