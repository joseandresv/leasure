from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Now as naive UTC — the form every timestamp column below stores."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    spotify_uri: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    youtube_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    isrc: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    artist: Mapped[str] = mapped_column(String(500))
    album: Mapped[str | None] = mapped_column(String(500), nullable=True)
    album_artist: Mapped[str | None] = mapped_column(String(500), nullable=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_discs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str] = mapped_column(String(16), default="mp3")
    quality: Mapped[str] = mapped_column(String(32), default="mp3_320")  # result tier: aac_256, mp3_320, flac_24_96...
    source_format_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    premium_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transcoded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_lossless: Mapped[bool] = mapped_column(Boolean, default=False)
    verification: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ok, duration_mismatch, unverified
    matched_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artwork_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="spotify")  # spotify, youtube, bandcamp, archive
    engine_used: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ytmusic, youtube-search, youtube, qobuz, card-import
    source_bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qobuz_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    qobuz_isrc: Mapped[str | None] = mapped_column(String(16), nullable=True)
    qobuz_match_method: Mapped[str | None] = mapped_column(String(8), nullable=True)   # id | isrc | search
    # not_configured|signed_out|ineligible|cap|no_match|not_streamable|unavailable|cdn_unavailable
    qobuz_skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    upgrade_state: Mapped[str | None] = mapped_column(String(16), nullable=True)   # None|queued|downloading|error|reverted
    upgrade_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    upgrade_backup_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    playlist_entries: Mapped[list["PlaylistTrack"]] = relationship(back_populates="track", cascade="all, delete-orphan")

    def upgrade_eligibility(self, *, include_unmatched: bool) -> str | None:
        """None when a FLAC upgrade may be queued, else the reason. No disk access."""
        if self.status != "done":
            return "not_done"
        if not self.file_path:
            return "no_file"
        if self.is_lossless:
            return "lossless"
        if self.engine_used == "qobuz":
            return "already_qobuz"
        if self.upgrade_state in ("queued", "downloading"):
            return "queued"
        if not include_unmatched and self.upgrade_state == "reverted":
            return "reverted"
        if not include_unmatched and self.qobuz_skip_reason in ("no_match", "not_streamable"):
            return "unmatched"
        return None

    def quality_readout(self) -> dict:
        """The provenance fields the quality_badge macro reads, for the views that
        hand templates plain dicts instead of the ORM row."""
        return {
            "quality": self.quality,
            "format": self.format,
            "premium_used": self.premium_used,
            "transcoded": self.transcoded,
            "is_lossless": self.is_lossless,
            "verification": self.verification,
            "matched_title": self.matched_title,
            "source_bitrate_kbps": self.source_bitrate_kbps,
            "source_codec": self.source_codec,
            "id": self.id,
            "source_bit_depth": self.source_bit_depth,
            "source_sample_rate": self.source_sample_rate,
            "source_format_id": self.source_format_id,
            "engine_used": self.engine_used,
            "match_score": self.match_score,
            "qobuz_match_method": self.qobuz_match_method,
            "qobuz_skip_reason": self.qobuz_skip_reason,
            "upgrade_state": self.upgrade_state,
            "upgrade_error": self.upgrade_error,
            "upgrade_eligible": self.upgrade_eligibility(include_unmatched=False) is None,
            "upgrade_possible": self.upgrade_eligibility(include_unmatched=True) is None,
            "upgrade_revertible": self.upgrade_backup_until is not None and self.upgrade_backup_until > utc_now(),
        }


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(32))  # spotify, youtube, manual
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    entries: Mapped[list["PlaylistTrack"]] = relationship(back_populates="playlist", cascade="all, delete-orphan")


class PlaylistTrack(Base):
    __tablename__ = "playlist_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)

    playlist: Mapped["Playlist"] = relationship(back_populates="entries")
    track: Mapped["Track"] = relationship(back_populates="playlist_entries")


class SyncHistory(Base):
    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_path: Mapped[str] = mapped_column(String(500))
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    tracks_added: Mapped[int] = mapped_column(Integer, default=0)
    tracks_removed: Mapped[int] = mapped_column(Integer, default=0)
    tracks_skipped: Mapped[int] = mapped_column(Integer, default=0)  # already current on the card, not re-copied
    total_size: Mapped[int] = mapped_column(Integer, default=0)


class CardCopy(Base):
    """A file a sync wrote, or found current, on a card: what is really there."""
    __tablename__ = "card_copies"
    __table_args__ = (UniqueConstraint("card_id", "rel_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[str] = mapped_column(String(36), index=True)          # .leasure-card.json
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"),
                                                 nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(8))                          # audio | jpg | lrc
    rel_path: Mapped[str] = mapped_column(Text)                           # POSIX, card-root relative
    rel_key: Mapped[str] = mapped_column(Text)                            # rel_path.casefold()
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)                           # source st_mtime given to the copy
    head_sha256: Mapped[str] = mapped_column(String(64))                  # sha256 of the first 1 MiB
    written_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SupersededFile(Base):
    """A card file an upgrade or revert made obsolete; the next sync removes it once the
    replacement is on that card and the bytes still match."""
    __tablename__ = "superseded_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"),
                                                 nullable=True, index=True)
    card_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # None: any card
    basis: Mapped[str] = mapped_column(String(12))                        # card_copy | library
    reason: Mapped[str] = mapped_column(String(12), default="upgrade")    # upgrade | revert
    kind: Mapped[str] = mapped_column(String(8), default="audio")
    rel_path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    head_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
