"""Albums as the library has them: stable keys, groups of rows, display values. DB only, no network.

The key normaliser is frozen on purpose: it does not call the Qobuz, card-import or sync
normalisers, so a change made for them never silently re-keys stored album info. Changing
`norm_v2` or `key_artist` output needs a KEY_VERSION bump (album_info.rekey_stale_rows)."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from hashlib import sha1
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Track
from services.album_merge import edition_fit
from services.artwork import album_cover_path, sidecar_art_path

KEY_VERSION = "2"
LOOSE_KEY = "loose"
LOOSE_NAME = "Loose songs"
KEY_PATTERN = re.compile(r"a-[0-9a-f]{20}|loose")
UNKNOWN_ALBUMS = frozenset({"unknown album", "unknown"})
_BENIGN_V2 = re.compile(r"\b(?:\d{4} remaster(?:ed)?(?: version)?|remaster(?:ed)?(?: \d{4})?(?: version)?"
                        r"|album version|explicit|clean|single version|original mix)\b")


@lru_cache(maxsize=4096)
def norm_v2(text: str | None) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    n = " ".join(re.sub(r"[\W_]+", " ", _BENIGN_V2.sub(" ", t)).split())
    return n or " ".join(re.sub(r"[\W_]+", " ", t).split()) or " ".join(t.split())


@lru_cache(maxsize=4096)
def key_artist(artist: str | None, album_artist: str | None) -> str:
    """The card-folder artist rule (frozen copy), then its first comma part."""
    credit, track = (album_artist or "").strip(), (artist or "").strip()
    if not credit or (credit == track and "," in credit):
        credit = track
    return norm_v2(credit.split(",")[0]) or "unknown artist"


@dataclass(frozen=True)
class AlbumKey:
    key: str
    artist_norm: str
    album_norm: str


@lru_cache(maxsize=8192)
def album_key(album: str | None, album_artist: str | None, artist: str | None) -> AlbumKey | None:
    """None for album-less rows, which all belong to the `loose` group."""
    raw = (album or "").strip()
    if not raw or raw.casefold() in UNKNOWN_ALBUMS:
        return None
    a, b = key_artist(artist, album_artist), norm_v2(raw)
    digest = sha1(f"{KEY_VERSION}\x1falbum\x1f{a}\x1f{b}".encode()).hexdigest()[:20]
    return AlbumKey(key="a-" + digest, artist_norm=a, album_norm=b)


def key_of(album: str | None, album_artist: str | None, artist: str | None) -> str:
    key = album_key(album, album_artist, artist)
    return key.key if key else LOOSE_KEY


def valid_key(key: str) -> bool:
    return KEY_PATTERN.fullmatch(key or "") is not None


def quality_label(tier: str | None) -> str:
    tier = (tier or "").lower()
    if tier.startswith("flac"):
        return "FLAC"
    if tier.startswith("opus"):
        return "Opus"
    codec, _, rate = tier.partition("_")
    if codec in ("aac", "mp3") and rate.isdigit():
        return f"{codec.upper()} {rate}"
    return tier.upper() or "Unknown"


def quality_mix(done_rows) -> list[dict]:
    counts = Counter(quality_label(row.quality) for row in done_rows)
    return [{"label": label, "count": count} for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


@dataclass
class LocalAlbum:
    key: str
    kind: str                          # album | loose
    name: str
    artist: str
    album_artist: str | None
    artist_norm: str
    year: int | None
    total_tracks: int | None
    total_tracks_values: frozenset[int]
    total_discs: int
    total_discs_known: int | None
    artist_spelling: str
    genre: str | None
    track_count: int
    downloaded_at: datetime | None
    cover_track_id: int | None
    quality_mix: list[dict] = field(default_factory=list)
    all_lossless: bool = False


def _most_common(values, tie_order=None):
    counts = Counter(v for v in values if v not in (None, ""))
    if not counts:
        return None
    best = max(counts.values())
    tied = [v for v in counts if counts[v] == best]
    return min(tied, key=tie_order) if tie_order else tied[0]


def _summarise(key: str, done: list) -> LocalAlbum:
    """Display values of a group from its done rows (each with Track's attributes)."""
    first_seen = {}
    for row in sorted(done, key=lambda r: (r.downloaded_at or datetime.max, r.id)):
        first_seen.setdefault(row.album, len(first_seen))
    loose = key == LOOSE_KEY
    album_artist = _most_common(row.album_artist for row in done)
    artist = album_artist or (_most_common((row.artist or "").split(",")[0].strip() for row in done) or "")
    discs = [row.total_discs for row in done] + [row.disc_number for row in done]
    total_tracks_values = frozenset(row.total_tracks for row in done if row.total_tracks)
    with_file = next((row.id for row in sorted(done, key=lambda r: r.id) if row.file_path), None)
    sample = done[0] if done else None
    return LocalAlbum(
        key=key, kind="loose" if loose else "album",
        name=LOOSE_NAME if loose else (_most_common((row.album for row in done), first_seen.get) or ""),
        artist="" if loose else artist,
        album_artist=None if loose else album_artist,
        artist_norm="" if loose or sample is None else key_artist(sample.artist, sample.album_artist),
        year=None if loose else _most_common(row.year for row in done),
        total_tracks=None if loose else _most_common(row.total_tracks for row in done),
        total_tracks_values=total_tracks_values,
        total_discs=max([d for d in discs if d] + [1]),
        total_discs_known=_most_common(row.total_discs for row in done),
        artist_spelling=_most_common(row.artist for row in done) or artist,
        genre=_most_common(row.genre for row in done),
        track_count=len(done),
        downloaded_at=max((row.downloaded_at for row in done if row.downloaded_at), default=None),
        cover_track_id=with_file,
        quality_mix=quality_mix(done),
        all_lossless=bool(done) and all(row.is_lossless for row in done),
    )


_SCAN_COLUMNS = (Track.id, Track.album, Track.album_artist, Track.artist, Track.year, Track.total_tracks,
                 Track.total_discs, Track.disc_number, Track.downloaded_at, Track.quality, Track.is_lossless,
                 Track.file_path, Track.genre)
_scan_cache: tuple[tuple, list[LocalAlbum]] | None = None


def clear_cache() -> None:
    global _scan_cache
    _scan_cache = None


async def scan(session: AsyncSession) -> list[LocalAlbum]:
    """Every group with at least one done row; regrouped only when a done row's scanned values change."""
    global _scan_cache
    rows = (await session.execute(select(*_SCAN_COLUMNS).where(Track.status == "done").order_by(Track.id))).all()
    fingerprint = tuple(tuple(row) for row in rows)
    if _scan_cache and _scan_cache[0] == fingerprint:
        return _scan_cache[1]
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(key_of(row.album, row.album_artist, row.artist), []).append(row)
    albums = [_summarise(key, members) for key, members in groups.items()]
    _scan_cache = (fingerprint, albums)
    return albums


def _sort_key(track: Track) -> tuple:
    return track.disc_number or 1, track.track_number or 9999, (track.title or "").casefold()


async def load(session: AsyncSession, key: str) -> tuple[LocalAlbum, list[Track]] | None:
    """The group and all its rows (any status), in disc/track order; None when no row of it is done."""
    if not valid_key(key):
        return None
    candidates = await session.execute(select(Track.id, Track.album, Track.album_artist, Track.artist))
    ids = [row.id for row in candidates if key_of(row.album, row.album_artist, row.artist) == key]
    if not ids:
        return None
    rows = sorted((await session.scalars(select(Track).where(Track.id.in_(ids)))).all(), key=_sort_key)
    done = [row for row in rows if row.status == "done"]
    if not done:
        return None
    return _summarise(key, done), rows


async def find_key(session: AsyncSession, name: str, artist: str) -> tuple[str, bool] | None:
    """The local album a provider album (name, credit) is: (key, exact). Containment only by edition words
    and only when exactly one local album of that artist fits."""
    wanted = album_key(name, artist, artist)
    if wanted is None:
        return None
    albums = await scan(session)
    if any(album.key == wanted.key for album in albums):
        return wanted.key, True
    fits = [album.key for album in albums
            if album.kind == "album" and album.artist_norm == wanted.artist_norm and edition_fit(album.name, name)]
    return (fits[0], False) if len(fits) == 1 else None


def cover_file(key: str, rows) -> Path | None:
    """A done row's sidecar, else the album's cached cover. Blocking."""
    for row in rows:
        if row.status == "done" and (path := sidecar_art_path(row.file_path)):
            return path
    path = album_cover_path(key)
    return path if path.is_file() else None
