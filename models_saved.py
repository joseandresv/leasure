"""Snapshots of what the owner saved on Spotify, Qobuz and YouTube Music (the Saved tab)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models import Base, utc_now


class _Described:
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uri: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    artist: Mapped[str | None] = mapped_column(String(500), nullable=True)
    album: Mapped[str | None] = mapped_column(String(500), nullable=True)
    album_source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    album_artist: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_discs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    isrc: Mapped[str | None] = mapped_column(String(16), nullable=True)
    readable: Mapped[bool] = mapped_column(Boolean, default=True)
    qobuz_max_bits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qobuz_max_khz: Mapped[float | None] = mapped_column(Float, nullable=True)
    qobuz_streamable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    group_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    match_track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True)
    match_state: Mapped[str | None] = mapped_column(String(12), nullable=True)   # library|other_album|queued|error
    matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SavedItem(_Described, Base):
    """One saved thing on one service: a liked song, a saved album or playlist, a followed artist."""
    __tablename__ = "saved_items"
    __table_args__ = (
        UniqueConstraint("source", "kind", "source_id"),
        Index("ix_saved_items_listing", "source", "kind", "removed_at", "added_at"),
        Index("ix_saved_items_position", "source", "kind", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(12))        # spotify | qobuz | youtube
    kind: Mapped[str] = mapped_column(String(10))          # track | album | playlist | artist
    track_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_is_me: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    snapshot_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)   # the service's latest version
    entries_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)    # the version saved_entries hold
    entries_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entries_matched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    search_text: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SavedEntry(_Described, Base):
    """A track of a saved album or playlist."""
    __tablename__ = "saved_entries"
    __table_args__ = (UniqueConstraint("collection_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("saved_items.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class SavedSyncState(Base):
    """Progress and schedule of one (source, kind) refresh."""
    __tablename__ = "saved_sync_state"
    __table_args__ = (UniqueConstraint("source", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(10), default="idle")   # idle|running|paused|blocked|error|off
    phase_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    done: Mapped[int] = mapped_column(Integer, default=0)
    total_remote: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_seen: Mapped[int] = mapped_column(Integer, default=0)
    placeholders: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)            # json
    full_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    full_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_incremental_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_full_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_mismatch_full_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # rate_limited|daily_budget|scope_missing|signed_out|not_connected|shape|check_failed|forbidden
    blocked_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resume_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pending_removals: Mapped[str | None] = mapped_column(Text, nullable=True)  # json ids, for the two-pass sweep
    scope_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SavedBatch(Base):
    """One "Download all": its songs are fed to the download queue one at a time, paced."""
    __tablename__ = "saved_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    scope_text: Mapped[str] = mapped_column(String(300), default="")
    format: Mapped[str] = mapped_column(String(16), default="native")
    requested: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(10), default="active")   # expanding|active|paused|done|cancelled
    include_other_album: Mapped[bool] = mapped_column(Boolean, default=False)
    counts: Mapped[str | None] = mapped_column(Text, nullable=True)      # json: the confirmation's numbers
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SavedBatchItem(Base):
    """A song of a batch (or a saved album or playlist whose songs are read later). No Track row exists until
    the feeder hands the song to the worker; `data` keeps what the row will be built from."""
    __tablename__ = "saved_batch_items"
    __table_args__ = (Index("ix_saved_batch_items_feed", "batch_id", "state", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("saved_batches.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10), default="track")      # track | collection
    saved_item_id: Mapped[int | None] = mapped_column(ForeignKey("saved_items.id", ondelete="SET NULL"), nullable=True)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("saved_entries.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    route: Mapped[str] = mapped_column(String(8), default="qobuz")      # qobuz | youtube
    quality: Mapped[str] = mapped_column(String(16), default="native")
    # waiting|expand|expanded|fed|done|error|skipped_library|skipped_other_album|skipped_queued|not_streamable|cancelled
    state: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True, index=True)
    group_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)        # json
    fed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
