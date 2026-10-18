"""Rebuild the library from the audio files on an H2 card: read their tags, look up
missing track/disc numbers, copy each song into the library under the name Leasure
would give it, and record it as a finished track. Card files are only ever read."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import async_session
from models import Playlist, PlaylistTrack, Track, utc_now
from services import spotify_client, tagger, youtube_client
from services.device import AUDIO_EXTS, artist_folder, build_device_path, resolve_sync_target
from services.playlist import PLAYLIST_EXT, read_manifest, sanitize_playlist_stem
from services.tracks import relocate_library_file
from services.yt_engine import _bitrate_tier, _quality_label, _track_stem, with_audio_suffix

logger = logging.getLogger(__name__)

SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information"}
UNKNOWN_ALBUM_DIRS = {"unknown album", "unknown"}
SIDECAR_SUFFIXES = (".jpg", ".lrc")
LOSSLESS_CODECS = {"flac", "alac", "wav"}

DUPLICATE_DURATION_MS = 3000
MATCH_DURATION_MS = 5000
FUZZY_TITLE_RATIO = 0.85
MIN_ALBUM_SIMILARITY = 0.8
MIN_MATCHED_SHARE = 0.6
COMMIT_EVERY = 25
SPOTIFY_PAUSE_S = 0.5
SPOTIFY_MAX_RETRY_AFTER_S = 30

_PREFIX = re.compile(r"^(?:(\d{1,2})-)?(\d{1,3}) - ")
_FEAT_BRACKET = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring|with)\b[^)\]]*[)\]]", re.IGNORECASE)
_FEAT_TRAILING = re.compile(r"\s+(?:feat|ft|featuring)\.?\s.*$", re.IGNORECASE)
_TAG_FIELDS = ("title", "artist", "album", "album_artist", "track_number", "total_tracks", "disc_number",
               "total_discs", "genre", "year")
_IDENTITIES = ("spotify_uri", "youtube_id", "isrc")


class ProviderThrottled(RuntimeError):
    """The provider answered 429; stop asking it for the rest of the run."""


@dataclass
class CardFile:
    rel: str
    path: Path
    size: int
    mtime: float
    title: str
    artist: str
    album: str | None
    album_artist: str | None
    track_number: int | None = None
    total_tracks: int | None = None
    disc_number: int | None = None
    total_discs: int | None = None
    genre: str | None = None
    year: int | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    codec: str | None = None
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    tagged: dict = field(default_factory=dict)
    spotify_uri: str | None = None
    youtube_id: str | None = None
    artwork_url: str | None = None
    matched_title: str | None = None
    filled: list[str] = field(default_factory=list)


def _text(value) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def _words(value: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", value).split())


def normalize_title(title: str | None) -> str:
    """Comparable form of a song title: no "NN - " prefix, no featured-artist credit, no punctuation."""
    text = _PREFIX.sub("", unicodedata.normalize("NFKC", title or ""))
    stripped = _FEAT_TRAILING.sub("", _FEAT_BRACKET.sub("", text))
    return _words(_text(stripped)) or _words(_text(text))


def similarity(a: str | None, b: str | None) -> float:
    return SequenceMatcher(None, _words(_text(a)), _words(_text(b))).ratio()


def card_key(rel: str) -> str:
    """How a playlist entry and a card path are compared: FAT is case-insensitive, Unicode forms vary."""
    return unicodedata.normalize("NFC", rel.replace("\\", "/").lstrip("/")).casefold()


# ---------------------------------------------------------------------------
# Reading the card
# ---------------------------------------------------------------------------

def _joined(value) -> str | None:
    if value is None:
        return None
    values = getattr(value, "text", value)
    if not isinstance(values, list):
        values = [values]
    text = ", ".join(str(v).strip() for v in values if str(v).strip())
    return text or None


def _number_pair(text: str | None) -> tuple[int | None, int | None]:
    match = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", text or "")
    if not match:
        return None, None
    return int(match.group(1)) or None, int(match.group(2) or 0) or None


def _year(text: str | None) -> int | None:
    match = re.match(r"\s*(\d{4})", text or "")
    return int(match.group(1)) if match else None


def _id3_fields(tags) -> dict:
    get = lambda key: _joined(tags.get(key)) if tags is not None else None  # noqa: E731
    track, total_tracks = _number_pair(get("TRCK"))
    disc, total_discs = _number_pair(get("TPOS"))
    return {"title": get("TIT2"), "artist": get("TPE1"), "album": get("TALB"), "album_artist": get("TPE2"),
            "track_number": track, "total_tracks": total_tracks, "disc_number": disc, "total_discs": total_discs,
            "genre": get("TCON"), "year": _year(get("TDRC") or get("TYER")), "isrc": get("TSRC")}


def _mp4_fields(tags) -> dict:
    tags = tags or {}
    get = lambda key: _joined(tags.get(key))  # noqa: E731
    track, total_tracks = (list(tags.get("trkn") or [(None, None)])[0] + (None, None))[:2]
    disc, total_discs = (list(tags.get("disk") or [(None, None)])[0] + (None, None))[:2]
    isrc = (tags.get("----:com.apple.iTunes:ISRC") or [b""])[0]
    return {"title": get("\xa9nam"), "artist": get("\xa9ART"), "album": get("\xa9alb"), "album_artist": get("aART"),
            "track_number": track or None, "total_tracks": total_tracks or None, "disc_number": disc or None,
            "total_discs": total_discs or None, "genre": get("\xa9gen"), "year": _year(get("\xa9day")),
            "isrc": bytes(isrc).decode("utf-8", "replace").strip() or None}


def _vorbis_fields(tags) -> dict:
    tags = tags if tags is not None else {}
    # Vorbis comment keys are case-insensitive and multi-valued; mutagen's dict handles both.
    get = lambda *keys: next((_joined(tags[k]) for k in keys if k in tags), None)  # noqa: E731
    track, total_tracks = _number_pair(get("tracknumber"))
    disc, total_discs = _number_pair(get("discnumber"))
    return {"title": get("title"), "artist": get("artist"), "album": get("album"), "album_artist": get("albumartist"),
            "track_number": track, "total_tracks": total_tracks or _number_pair(get("tracktotal", "totaltracks"))[0],
            "disc_number": disc, "total_discs": total_discs or _number_pair(get("disctotal", "totaldiscs"))[0],
            "genre": get("genre"), "year": _year(get("date", "year")), "isrc": get("isrc")}


def read_tags(path: Path) -> tuple[dict, dict]:
    """(tag fields, stream info) of an audio file, whatever its container."""
    import mutagen
    from mutagen.id3 import ID3
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4

    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("mutagen cannot read this file")
    codec = "mp3" if isinstance(audio, MP3) else type(audio).__name__.lower().removeprefix("ogg")
    if isinstance(audio, MP4):
        tags, codec = _mp4_fields(audio.tags), getattr(audio.info, "codec", None) or "aac"
    elif isinstance(audio.tags, ID3):
        tags = _id3_fields(audio.tags)
    elif isinstance(audio, MP3):
        tags = _id3_fields(None)
    else:
        tags = _vorbis_fields(audio.tags)
    kbps = (getattr(audio.info, "bitrate", 0) or 0) / 1000
    info = {"duration_ms": round((audio.info.length or 0) * 1000) or None, "codec": codec,
            # Lossy rates snap to the tiers downloads record; lossless ones are far above them.
            "bitrate_kbps": (_bitrate_tier(kbps) if kbps <= 330 else round(kbps)) if kbps else None,
            "sample_rate": getattr(audio.info, "sample_rate", None)}
    return tags, info


def read_card_file(root: Path, path: Path) -> CardFile:
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    tags, info = read_tags(path)
    stat = path.stat()
    prefix = _PREFIX.match(path.stem)
    album_dir = parts[-2] if len(parts) >= 3 else None
    folder_album = album_dir if album_dir and album_dir.casefold() not in UNKNOWN_ALBUM_DIRS else None
    card = CardFile(
        rel=rel, path=path, size=stat.st_size, mtime=stat.st_mtime,
        title=tags["title"] or _PREFIX.sub("", path.stem),
        artist=tags["artist"] or tags["album_artist"] or (parts[0] if len(parts) >= 2 else "Unknown Artist"),
        album=tags["album"] or folder_album, album_artist=tags["album_artist"],
        **{k: tags[k] for k in ("track_number", "total_tracks", "disc_number", "total_discs", "genre", "year", "isrc")},
        **info, tagged={k: tags[k] for k in _TAG_FIELDS},
    )
    if card.track_number is None and prefix:
        card.track_number = int(prefix.group(2)) or None
        card.disc_number = card.disc_number or (int(prefix.group(1)) if prefix.group(1) else None)
        card.filled.append("track_number (filename)")
    return card


def scan_card(root: Path) -> tuple[list[CardFile], list[dict]]:
    """Every audio file under the card's artist folders, with the files it could not read."""
    found, errors = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if Path(dirpath) == root:
            continue
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in AUDIO_EXTS:
                continue
            try:
                found.append(read_card_file(root, path))
            except Exception as e:
                errors.append({"card": path.relative_to(root).as_posix(), "error": str(e)[:200]})
    return found, errors


def group_by_album(files: list[CardFile]) -> dict[str, list[CardFile]]:
    albums: dict[str, list[CardFile]] = defaultdict(list)
    for f in files:
        albums[f.rel.rsplit("/", 1)[0]].append(f)
    return dict(sorted(albums.items()))


def _keep_rank(f: CardFile) -> tuple:
    return ((f.codec or "").lower() in LOSSLESS_CODECS, f.bitrate_kbps or 0, f.track_number is not None, f.size)


def _carry_numbers(kept: CardFile, others: list[CardFile]) -> None:
    """The better copy keeps the song's place in the album when only a dropped copy was numbered."""
    donor = max((f for f in others if f.track_number), key=_keep_rank, default=None)
    if donor is None or kept.track_number not in (None, donor.track_number):
        return
    for name in ("track_number", "disc_number"):
        if getattr(kept, name) is None and getattr(donor, name):
            setattr(kept, name, getattr(donor, name))
            kept.filled.append(f"{name} (duplicate copy)")


def dedupe_album(files: list[CardFile]) -> tuple[list[CardFile], list[tuple[CardFile, CardFile]]]:
    """One file per song: the same normalised title and length is the same recording.
    Returns (kept, [(dropped, kept_instead)])."""
    clusters: list[list[CardFile]] = []
    for f in files:
        key = normalize_title(f.title)
        home = next((c for c in clusters if normalize_title(c[0].title) == key
                     and abs((c[0].duration_ms or 0) - (f.duration_ms or 0)) <= DUPLICATE_DURATION_MS), None)
        if home is None:
            clusters.append([f])
        else:
            home.append(f)
    kept, dropped = [], []
    for cluster in clusters:
        best = max(cluster, key=_keep_rank)
        others = [f for f in cluster if f is not best]
        _carry_numbers(best, others)
        kept.append(best)
        dropped += [(f, best) for f in others]
    return kept, dropped


def infer_total_discs(files: list[CardFile]) -> None:
    """A disc 2 in the folder means a multi-disc album even when no file says how many discs."""
    discs = [f.disc_number for f in files if f.disc_number]
    if not discs or max(discs) < 2:
        return
    for f in files:
        # Without its own disc number a file would be filed under disc 1 of a multi-disc album.
        if f.disc_number and not f.total_discs:
            f.total_discs = max(discs)
            f.filled.append("total_discs (inferred)")


# ---------------------------------------------------------------------------
# Looking albums up
# ---------------------------------------------------------------------------

def _closest(items: list[dict], album: str, artist: str, name, artists, limit: int = 2) -> list[dict]:
    scored = [(similarity(album, name(i)) + 0.5 * similarity(artist, artists(i)), i) for i in items
              if similarity(album, name(i)) >= 0.6]
    return [i for _, i in sorted(scored, key=lambda pair: -pair[0])[:limit]]


def _spotify_payload(data: dict) -> dict:
    album = data["album"]
    return {"provider": "spotify", "id": album["id"], "name": album["name"], "artist": album.get("artist"),
            "artwork_url": album.get("image_url"), "total_tracks": album.get("total_tracks"),
            "total_discs": album.get("total_discs"),
            "tracks": [{"title": t["name"], "duration_ms": t.get("duration_ms"), "track_number": t.get("track_number"),
                        "disc_number": t.get("disc_number"), "isrc": t.get("isrc"), "spotify_uri": t.get("uri"),
                        "youtube_id": None} for t in data["tracks"]]}


def _spotify_lookup_client():
    base = spotify_client.get_client()
    if base is None:
        return None
    import requests
    import spotipy

    # A plain session, without spotipy's urllib3 retry adapter: that adapter sleeps through the
    # whole Retry-After of a 429, which Spotify sets to many minutes once a bulk run trips its limit.
    return spotipy.Spotify(auth_manager=base.auth_manager, requests_session=requests.Session())


def _spotify_call(fn, *args, **kwargs):
    """One paced Spotify request; a short Retry-After is waited out once, a long one ends Spotify lookups."""
    import spotipy

    for attempt in (1, 2):
        time.sleep(SPOTIFY_PAUSE_S)
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as e:
            if e.http_status != 429:
                raise
            wait = int((e.headers or {}).get("Retry-After") or 0)
            if attempt == 2 or wait > SPOTIFY_MAX_RETRY_AFTER_S:
                raise ProviderThrottled(f"spotify asked to wait {wait} s") from e
            time.sleep(wait or 1)


def spotify_album_candidates(artist: str, album: str) -> list[dict] | None:
    """Up to two Spotify albums that look like `album`, with their tracks; None when Spotify is not connected."""
    client = _spotify_lookup_client()
    if client is None:
        return None
    clean = lambda s: s.replace('"', " ")  # noqa: E731
    found = _spotify_call(client.search, q=f'album:"{clean(album)}" artist:"{clean(artist)}"', type="album", limit=5)
    items = (found.get("albums") or {}).get("items") or []
    if not items:
        found = _spotify_call(client.search, q=f"{artist} {album}", type="album", limit=5)
        items = (found.get("albums") or {}).get("items") or []
    payloads = []
    for item in _closest(items, album, artist, lambda a: a.get("name"),
                         lambda a: ", ".join(x["name"] for x in a.get("artists") or [])):
        data = _spotify_call(spotify_client.get_album_tracks, item["id"], client=client)
        if data:
            payloads.append(_spotify_payload(data))
    return payloads


def _youtube_payload(browse_id: str, data: dict) -> dict:
    album = data["album"]
    return {"provider": "youtube", "id": browse_id, "name": album["name"], "artist": album.get("artist"),
            "artwork_url": album.get("image_url"), "total_tracks": album.get("total_tracks"), "total_discs": None,
            "tracks": [{"title": t["name"], "duration_ms": t.get("duration_ms"), "track_number": t.get("track_number"),
                        "disc_number": None, "isrc": None, "spotify_uri": None, "youtube_id": t.get("id")}
                       for t in data["tracks"]]}


def youtube_album_candidates(artist: str, album: str) -> list[dict] | None:
    """Up to two YouTube Music albums that look like `album`. Searching needs no login."""
    from ytmusicapi import YTMusic

    client = youtube_client.get_client() or YTMusic()
    results = client.search(f"{artist} {album}", filter="albums", limit=5) or []
    payloads = []
    for item in _closest([r for r in results if r.get("browseId")], album, artist, lambda r: r.get("title"),
                         lambda r: ", ".join(a["name"] for a in r.get("artists") or [] if a.get("name"))):
        data = youtube_client.get_album_tracks(item["browseId"], client=client)
        if data:
            payloads.append(_youtube_payload(item["browseId"], data))
    return payloads


PROVIDERS = {"spotify": spotify_album_candidates, "youtube": youtube_album_candidates}


class AlbumLookup:
    """Provider album candidates, cached on disk so a rerun asks nobody twice."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.disabled: dict[str, str] = {}
        try:
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    async def candidates(self, provider: str, artist: str, album: str) -> list[dict]:
        key = f"{provider}|{_text(artist)}|{_text(album)}"
        if key in self.cache:
            return self.cache[key]
        if provider in self.disabled:
            return []
        try:
            found = await asyncio.to_thread(PROVIDERS[provider], artist, album)
        except ProviderThrottled as e:
            self.disabled[provider] = f"rate limited: {e}"
            logger.warning("%s is rate limiting; no more %s lookups this run", provider, provider)
            return []
        except Exception as e:
            logger.warning("%s lookup failed for '%s - %s': %s", provider, artist, album, e)
            return []
        if found is None:
            self.disabled[provider] = "not connected"
            return []
        self.cache[key] = found
        self._save()
        return found

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cache_path)


@dataclass
class AlbumMatch:
    candidate: dict
    pairs: list[tuple[CardFile, dict, bool]]
    album_similarity: float
    confident: bool
    ambiguous: list[CardFile] = field(default_factory=list)

    def summary(self) -> dict:
        return {"provider": self.candidate["provider"], "id": self.candidate["id"], "name": self.candidate["name"],
                "matched": len(self.pairs), "album_similarity": round(self.album_similarity, 2)}


def _durations_agree(a: int | None, b: int | None) -> bool:
    return bool(a and b) and abs(a - b) <= MATCH_DURATION_MS


def _pair_rank(f: CardFile, t: dict, ratio: float) -> tuple:
    same_number = f.track_number is not None and t.get("track_number") == f.track_number and (
        f.disc_number is None or t.get("disc_number") in (None, f.disc_number))
    return ratio, same_number, -abs(f.duration_ms - t["duration_ms"])


def _pair_tracks(files: list[CardFile], tracks: list[dict]) -> tuple[list[tuple[CardFile, dict, bool]], list[CardFile]]:
    """Card file -> provider track, exact titles first, then close ones; durations must agree. Among equal
    titles the file's own number, then the closer length, decides; an unnumbered file facing several equal
    titles is ambiguous and stays unpaired. Returns (pairs, ambiguous files)."""
    pairs, used, settled, ambiguous = [], set(), set(), []
    numbered_first = sorted(files, key=lambda f: f.track_number is None)
    for exact in (True, False):
        for f in numbered_first:
            if id(f) in settled:
                continue
            wanted = normalize_title(f.title)
            scored = []
            for i, t in enumerate(tracks):
                if i in used or not _durations_agree(f.duration_ms, t.get("duration_ms")):
                    continue
                ratio = 1.0 if normalize_title(t["title"]) == wanted else SequenceMatcher(
                    None, wanted, normalize_title(t["title"])).ratio()
                if ratio == 1.0 if exact else ratio >= FUZZY_TITLE_RATIO:
                    scored.append((_pair_rank(f, t, ratio), i))
            if not scored:
                continue
            settled.add(id(f))
            best_rank, best = max(scored, key=lambda s: s[0])
            if f.track_number is None and sum(rank[0] == best_rank[0] for rank, _ in scored) > 1:
                ambiguous.append(f)
                continue
            used.add(best)
            pairs.append((f, tracks[best], exact))
    return pairs, ambiguous


def match_album(files: list[CardFile], album: str, candidate: dict) -> AlbumMatch:
    pairs, ambiguous = _pair_tracks(files, candidate["tracks"])
    album_similarity = similarity(album, candidate["name"])
    needed = max(1, math.ceil(MIN_MATCHED_SHARE * len(files)))
    return AlbumMatch(candidate, pairs, album_similarity,
                      album_similarity >= MIN_ALBUM_SIMILARITY and len(pairs) >= needed, ambiguous)


async def best_match(lookup: AlbumLookup, files: list[CardFile], artist: str, album: str) -> AlbumMatch | None:
    """Spotify first, then YouTube Music; the best confident match, else the best attempt (not confident)."""
    best = None
    for provider in PROVIDERS:
        for candidate in await lookup.candidates(provider, artist, album):
            match = match_album(files, album, candidate)
            if best is None or (match.confident, len(match.pairs), match.album_similarity) > (
                    best.confident, len(best.pairs), best.album_similarity):
                best = match
        if best and best.confident:
            return best
    return best


def number_conflicts(match: AlbumMatch) -> list[dict]:
    """Numbers the card has that an exact title+length pair disagrees with."""
    return [{"card": f.rel, "field": name, "card_value": getattr(f, name), "lookup_value": t[name],
             "provider": match.candidate["provider"]}
            for f, t, exact in match.pairs if exact
            for name in ("track_number", "disc_number")
            if t.get(name) and getattr(f, name) not in (None, t[name]) and name not in f.filled]


def doubtful_release(files: list[CardFile], match: AlbumMatch, conflicts: list[dict]) -> bool:
    """Card numbers contradicting most pairs (any pair, in a one- or two-file folder) point at another
    release of the same name, whose numbers, totals and ids are wrong for these files."""
    disputed = {c["card"] for c in conflicts}
    return bool(disputed) and (len(files) <= 2 or 2 * len(disputed) > len(match.pairs))


def _fill_track_number(f: CardFile, t: dict, taken: set, flat_multi_disc: bool, provider: str) -> dict | None:
    """Give an unnumbered file its pair's number, or return why it was held back."""
    slot = (f.disc_number or t.get("disc_number") or 1, t["track_number"])
    if flat_multi_disc and f.disc_number != 1:
        reason = "multi-disc album, but the release numbers its tracks without discs"
    elif slot in taken:
        reason = "number already used by another file in the folder"
    else:
        taken.add(slot)
        f.track_number = t["track_number"]
        f.filled.append("track_number")
        return None
    return {"card": f.rel, "field": "track_number", "card_value": None, "lookup_value": t["track_number"],
            "provider": provider, "reason": reason}


def apply_match(files: list[CardFile], match: AlbumMatch) -> list[dict]:
    """Fill the numbers and ids the card lacks; report, never change, numbers an exact match disagrees with,
    and take nothing from a pair that disagrees (nor anything at all from a doubtful release)."""
    candidate, conflicts = match.candidate, number_conflicts(match)
    if doubtful_release(files, match, conflicts):
        return conflicts
    disputed = {c["card"] for c in conflicts}
    taken = {(f.disc_number or 1, f.track_number) for f in files if f.track_number}
    # A release without disc numbers (YouTube Music) numbers a multi-disc album 1..N straight through,
    # which is only right for disc 1.
    flat_multi_disc = not any(t.get("disc_number") for t in candidate["tracks"]) and any(
        (f.disc_number or 0) > 1 or (f.total_discs or 0) > 1 for f in files)
    for f, t, _exact in match.pairs:
        if f.rel in disputed:
            continue
        if f.track_number is None and t.get("track_number"):
            held_back = _fill_track_number(f, t, taken, flat_multi_disc, candidate["provider"])
            if held_back:
                conflicts.append(held_back)
        if f.disc_number is None and t.get("disc_number"):
            f.disc_number = t["disc_number"]
            f.filled.append("disc_number")
        for name in _IDENTITIES:
            if t.get(name) and not getattr(f, name):
                setattr(f, name, t[name])
        f.matched_title = t["title"]
        f.artwork_url = f.artwork_url or candidate.get("artwork_url")
    for f in files:
        if f.total_tracks is None and candidate.get("total_tracks"):
            f.total_tracks = candidate["total_tracks"]
            f.filled.append("total_tracks")
        # Without its own disc number a file would be filed under disc 1 of a multi-disc album.
        if f.total_discs is None and candidate.get("total_discs") and (f.disc_number or candidate["total_discs"] == 1):
            f.total_discs = candidate["total_discs"]
            f.filled.append("total_discs")
    return conflicts


# ---------------------------------------------------------------------------
# Library rows and files
# ---------------------------------------------------------------------------

def _quality(codec: str | None, bitrate_kbps: int | None) -> tuple[str, bool]:
    family = (codec or "").lower()
    if family in LOSSLESS_CODECS:
        return f"{family}_lossless", True
    return _quality_label(codec, bitrate_kbps), False


def build_track(f: CardFile) -> Track:
    """The row a card file becomes. What the card cannot tell (Premium, transcoding) stays unknown."""
    quality, lossless = _quality(f.codec, f.bitrate_kbps)
    track = Track(
        title=f.title, artist=f.artist, album=f.album, album_artist=f.album_artist, genre=f.genre,
        track_number=f.track_number, disc_number=f.disc_number, total_tracks=f.total_tracks,
        total_discs=f.total_discs, duration_ms=f.duration_ms, year=f.year, format=f.path.suffix.lower().lstrip("."),
        quality=quality, is_lossless=lossless, source_codec=f.codec, source_bitrate_kbps=f.bitrate_kbps,
        source_sample_rate=f.sample_rate, premium_used=None, transcoded=None, verification="imported",
        matched_title=f.matched_title, artwork_url=f.artwork_url, source="card", engine_used="card-import",
        status="done", file_size=f.size, downloaded_at=datetime.fromtimestamp(f.mtime, UTC).replace(tzinfo=None),
        spotify_uri=f.spotify_uri, youtube_id=f.youtube_id, isrc=f.isrc,
    )
    return track


def tag_view(track: Track) -> dict:
    """The tag values the tagger writes for `track`, in the shape CardFile.tagged records."""
    return {"title": track.title, "artist": track.artist, "album": track.album,
            "album_artist": track.album_artist or tagger.primary_artist(track.artist),
            "track_number": track.track_number, "total_tracks": track.total_tracks, "disc_number": track.disc_number,
            "total_discs": track.total_discs, "genre": tagger.primary_genre(track.genre), "year": track.year}


def device_rel(track: Track, ext: str) -> str:
    return build_device_path(track.artist, track.album, track.track_number, track.title, ext,
                             album_artist=track.album_artist, disc_number=track.disc_number,
                             total_discs=track.total_discs)


def _row_device_rel(row: Track) -> str:
    ext = Path(row.file_path).suffix.lstrip(".") if row.file_path else row.format
    return device_rel(row, ext or "mp3")


def _song_key(track: Track) -> tuple[str, str, str]:
    return (artist_folder(track.artist, track.album_artist).casefold(), _text(track.album or ""),
            normalize_title(track.title))


def _path_key(path: str | Path) -> str:
    # The library may sit on drvfs/NTFS, where names differing only in case are one file.
    return os.path.normcase(os.path.abspath(path)).casefold()


def _same_recording(track: Track, row: Track) -> bool:
    """What dedupe_album calls one song: lengths agree and no disc/track number contradicts."""
    # Card rows carry the length measured from the file; other rows the provider's, which may differ more.
    tolerance = DUPLICATE_DURATION_MS if row.source == "card" else MATCH_DURATION_MS
    if track.duration_ms and row.duration_ms and abs(track.duration_ms - row.duration_ms) > tolerance:
        return False
    return all(getattr(track, name) is None or getattr(row, name) is None or getattr(track, name) == getattr(row, name)
               for name in ("track_number", "disc_number"))


class LibraryIndex:
    """The rows a card file could already be: by provider ids, by song, and by library file."""

    def __init__(self, rows: list[Track]):
        self.by: dict[str, dict] = {name: {} for name in _IDENTITIES}
        self.songs: dict[tuple, list[Track]] = defaultdict(list)
        self.paths: dict[str, Track] = {}
        for row in rows:
            self.add(row)

    def add(self, row: Track) -> None:
        for name in _IDENTITIES:
            self.add_identity(name, getattr(row, name), row)
        self.songs[_song_key(row)].append(row)
        if row.file_path:
            self.paths.setdefault(_path_key(row.file_path), row)

    def add_identity(self, name: str, value: str | None, row: Track) -> None:
        if value:
            self.by[name].setdefault(value, row)

    def find(self, track: Track) -> Track | None:
        for name in _IDENTITIES:
            value = getattr(track, name)
            if value and value in self.by[name]:
                return self.by[name][value]
        return next((row for row in self.songs.get(_song_key(track), []) if _same_recording(track, row)), None)

    def owner(self, path: Path) -> Track | None:
        return self.paths.get(_path_key(path))


@dataclass
class Planned:
    file: CardFile
    track: Track
    dest: Path
    device_rel: str
    retag: bool
    outcome: str
    row: Track | None = None
    fills: list[str] = field(default_factory=list)


def _outcome(existing: Track | None) -> str:
    if existing is None:
        return "import"
    if existing.id is None:
        # Planned earlier in this run from another card folder: the same song twice.
        return "existing"
    if existing.status in ("pending", "downloading"):
        return "queued"
    if existing.status == "done" and existing.file_path and Path(existing.file_path).exists():
        return "existing"
    return "adopt"


_ENRICHABLE = (*_IDENTITIES, "track_number", "disc_number", "total_tracks", "total_discs")


def enrichable(row: Track, track: Track) -> list[str]:
    """The ids and numbers a row lacks that the card file's lookup has now."""
    return [name for name in _ENRICHABLE if not getattr(row, name) and getattr(track, name)]


def plan_file(f: CardFile, index: LibraryIndex) -> Planned:
    """Where a kept card file goes and whether the library already has it (thread-side: stats files)."""
    track = build_track(f)
    dest = with_audio_suffix(_track_stem(track), f".{track.format}")
    rel = device_rel(track, track.format)
    retag = tag_view(track) != f.tagged and dest.suffix in tagger.TAGGABLE_SUFFIXES
    existing = index.find(track)
    outcome = _outcome(existing)
    owner = index.owner(dest)
    if outcome in ("import", "adopt") and owner is not None and owner is not existing:
        return Planned(f, track, dest, rel, retag, "collision", owner)
    if outcome == "existing" and existing.id is not None and existing.source == "card":
        # An earlier import made this row; what its lookup missed (throttled, say) can be filled now.
        fills = enrichable(existing, track)
        if fills:
            for name in set(fills) & set(_IDENTITIES):
                index.add_identity(name, getattr(track, name), existing)
            return Planned(f, track, dest, rel, retag, "enrich", existing, fills)
    track.file_path = str(dest)
    track.synced_at = utc_now() if card_key(rel) == card_key(f.rel) and not retag else None
    if outcome == "import":
        index.add(track)
    elif outcome == "adopt":
        index.paths.setdefault(_path_key(dest), existing)
    return Planned(f, track, dest, rel, retag, outcome, existing)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(src: Path, dest: Path) -> None:
    """Copy through a .part file and only put it in place once size and SHA-256 match the source."""
    part = dest.with_name(f"{dest.name}.part")
    digest = hashlib.sha256()
    with open(src, "rb") as reader, open(part, "wb") as writer:
        while chunk := reader.read(1 << 20):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if part.stat().st_size != src.stat().st_size or file_sha256(part) != digest.hexdigest():
        part.unlink(missing_ok=True)
        raise OSError(f"the copy of {src.name} does not match the card file")
    os.replace(part, dest)


def _keep_mtime(path: Path, mtime: float) -> None:
    try:
        os.utime(path, (mtime, mtime))
    except OSError as e:
        logger.debug("Could not set the mtime of %s: %s", path, e)


def install_file(plan: Planned) -> int:
    """Copy one card file and its sidecars into the library, retagging offline when the
    metadata changed. Returns the library file's size. Thread-side; the card is only read."""
    f, dest = plan.file, plan.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    copy_verified(f.path, dest)
    for suffix in SIDECAR_SUFFIXES:
        sidecar = f.path.with_suffix(suffix)
        if sidecar.exists():
            shutil.copyfile(sidecar, dest.with_suffix(suffix))
            _keep_mtime(dest.with_suffix(suffix), sidecar.stat().st_mtime)
        elif suffix == ".jpg":
            tagger._export_embedded_art(dest, dest.with_suffix(".jpg"))
    if plan.retag:
        try:
            tagger._write_tags(dest, plan.track, strict=True)
        except Exception:
            for path in (dest, *(dest.with_suffix(suffix) for suffix in SIDECAR_SUFFIXES)):
                path.unlink(missing_ok=True)
            raise
    else:
        # Unchanged bytes keep the card's mtime, so the next sync sees the card copy as current.
        _keep_mtime(dest, f.mtime)
    return dest.stat().st_size


def relocate_and_retag(current: Path, stem: Path, row: Track) -> tuple[str, int]:
    """Rename an enriched row's library file to its new numbering and rewrite its tags; the rename
    is undone when the tags cannot be written. Returns (path, size). Thread-side."""
    path = relocate_library_file(current, stem)
    if path is None:
        raise FileNotFoundError(f"{current} is no longer in the library")
    try:
        tagger._write_tags(path, row, strict=True)
    except Exception:
        if path != current:
            relocate_library_file(path, current.with_name(current.stem))
        raise
    return str(path), path.stat().st_size


async def enrich_row(row: Track, track: Track) -> list[str]:
    """Fill what the row lacks from `track` (never overwriting), then rename and retag its file to match."""
    fills = enrichable(row, track)
    if not fills:
        return []
    before = {name: getattr(row, name) for name in fills}
    for name in fills:
        setattr(row, name, getattr(track, name))
    try:
        row.file_path, row.file_size = await asyncio.to_thread(relocate_and_retag, Path(row.file_path),
                                                               _track_stem(row), row)
    except Exception:
        for name, value in before.items():
            setattr(row, name, value)
        raise
    row.synced_at = None
    return fills


_ADOPTED_SKIP = {"id", "created_at", *_IDENTITIES}


def adopt_into(row: Track, track: Track) -> None:
    """Give an existing row (failed or file-less) the card file's metadata, keeping the ids it has."""
    for column in Track.__table__.columns:
        if column.name not in _ADOPTED_SKIP:
            setattr(row, column.name, getattr(track, column.name))
    for name in _IDENTITIES:
        if not getattr(row, name) and getattr(track, name):
            setattr(row, name, getattr(track, name))
    row.error_message = None


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def read_playlist(path: Path) -> list[str]:
    """Entries of an .m3u8 (UTF-8 with or without BOM, any line ending), comments dropped."""
    text = path.read_text(encoding="utf-8-sig")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def backup_playlist(path: Path, backup_dir: Path) -> Path:
    """A byte-for-byte copy of a card playlist, one per distinct content: the next sync rewrites the
    card's copy with only the entries the library resolved."""
    data = path.read_bytes()
    target = backup_dir / f"{path.stem}.{hashlib.sha256(data).hexdigest()[:12]}{path.suffix}"
    if not target.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target


def card_playlists(root: Path, backup_dir: Path) -> list[tuple[str, list[str], Path]]:
    """The .m3u8 playlists at the card root with their backups; older .m3u files are left alone."""
    return [(p.stem, read_playlist(p), backup_playlist(p, backup_dir)) for p in sorted(root.iterdir())
            if p.is_file() and p.suffix.lower() == PLAYLIST_EXT]


async def _store_playlist(session: AsyncSession, name: str, tracks: list[Track]) -> None:
    playlist = await session.scalar(select(Playlist).where(Playlist.source == "card", Playlist.source_id == name))
    if playlist is None:
        playlist = Playlist(name=name, source="card", source_id=name)
        session.add(playlist)
        await session.flush()
    else:
        await session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id))
    stored = [t for t in tracks if t.id is not None]
    session.add_all(PlaylistTrack(playlist_id=playlist.id, track_id=t.id, position=i) for i, t in enumerate(stored))
    await session.commit()


async def import_playlists(session: AsyncSession, root: Path, backup_dir: Path, resolve: dict[str, Track | None],
                           report: "ImportReport", dry_run: bool) -> None:
    for name, entries, backup in await asyncio.to_thread(card_playlists, root, backup_dir):
        tracks = [resolve.get(card_key(entry)) for entry in entries]
        summary = {"name": name, "entries": len(entries), "resolved": sum(t is not None for t in tracks),
                   "unresolved": [e for e, t in zip(entries, tracks, strict=True) if t is None], "backup": str(backup)}
        if sanitize_playlist_stem(name) != name:
            summary["note"] = f"sync will write it as {sanitize_playlist_stem(name)}{PLAYLIST_EXT}"
        clash = await session.scalar(select(Playlist.id).where(Playlist.name == name, Playlist.source != "card"))
        if clash:
            summary["skipped"] = "another playlist in the library already has this name"
        elif not summary["resolved"]:
            summary["skipped"] = "none of its entries is in the library"
        elif not dry_run:
            await _store_playlist(session, name, [t for t in tracks if t is not None])
        report.add("playlists", summary)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class ImportReport:
    source: str
    dry_run: bool
    library_dir: str
    scanned: int = 0
    lists: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    lookup_disabled: dict[str, str] = field(default_factory=dict)
    playlists_skipped: str | None = None

    def add(self, name: str, *items) -> None:
        self.lists[name].extend(items)

    def counts(self) -> dict:
        n = lambda name: len(self.lists.get(name, []))  # noqa: E731
        playlists = self.lists.get("playlists", [])
        return {
            "scanned": self.scanned, "read_errors": n("read_errors"), "imported": n("imported"),
            "adopted": n("adopted"), "deduped": n("duplicates"), "skipped_existing": n("skipped_existing"),
            "enriched": n("enriched"), "queued": n("queued"), "ambiguous": n("ambiguous"),
            "numbers_filled": n("numbers_filled"), "retagged": n("retagged"), "conflicts": n("conflicts"),
            "albums_matched": n("albums_matched"), "low_confidence_albums": n("low_confidence_albums"),
            "albums_not_looked_up": n("albums_not_looked_up"), "collisions": n("collisions"), "errors": n("errors"),
            "playlists": len(playlists), "playlist_entries": sum(p["entries"] for p in playlists),
            "playlist_entries_resolved": sum(p["resolved"] for p in playlists),
            "stale_card_copies": n("stale_card_copies"),
        }

    def to_dict(self) -> dict:
        return {"source": self.source, "dry_run": self.dry_run, "library_dir": self.library_dir,
                "counts": self.counts(), "lookup_disabled": self.lookup_disabled,
                "playlists_skipped": self.playlists_skipped, **dict(self.lists)}

    def summary(self) -> str:
        c = self.counts()
        verb = "would import" if self.dry_run else "imported"
        lines = [
            f"Card {self.source} -> library {self.library_dir}{' (dry run: nothing written)' if self.dry_run else ''}",
            f"  {c['scanned']} audio files scanned, {c['read_errors']} unreadable",
            f"  {verb} {c['imported']}, adopted into existing rows {c['adopted']}, "
            f"already in the library {c['skipped_existing']} (ids/numbers added to {c['enriched']}), "
            f"duplicate card copies {c['deduped']}",
            f"  {c['queued']} still queued for download in the library (their card copies are not stale)",
            f"  numbers filled on {c['numbers_filled']} files, {c['retagged']} retagged, {c['conflicts']} number conflicts, "
            f"{c['ambiguous']} ambiguous",
            f"  albums matched {c['albums_matched']}, low confidence {c['low_confidence_albums']}, "
            f"not looked up {c['albums_not_looked_up']}",
            f"  collisions {c['collisions']}, errors {c['errors']}",
            f"  playlists not imported: {self.playlists_skipped}" if self.playlists_skipped else
            f"  playlists {c['playlists']}: {c['playlist_entries_resolved']}/{c['playlist_entries']} entries resolved",
            f"  {c['stale_card_copies']} card files become stale after the next sync; remove them afterwards with "
            f"python -m scripts.clean_card",
        ]
        lines += [f"  {provider} lookups stopped: {why}" for provider, why in self.lookup_disabled.items()]
        return "\n".join(lines)


async def _lookup_album(lookup: AlbumLookup, folder: str, files: list[CardFile], report: ImportReport) -> None:
    albums = Counter(f.album for f in files if f.album)
    if not albums:
        report.add("albums_not_looked_up", {"folder": folder, "reason": "no album name"})
        return
    album = albums.most_common(1)[0][0]
    artist = Counter(tagger.primary_artist(f.album_artist or f.artist) for f in files).most_common(1)[0][0] or ""
    match = await best_match(lookup, files, artist, album)
    low_confidence = {"folder": folder, "artist": artist, "album": album, "tracks": len(files),
                      "best": match.summary() if match else None}
    if match is not None:
        report.add("ambiguous", *({"card": f.rel, "provider": match.candidate["provider"],
                                   "reason": "several tracks of the release share its title and length; no number filled"}
                                  for f in match.ambiguous))
    if match is None or not match.confident:
        report.add("low_confidence_albums", low_confidence)
        return
    conflicts = number_conflicts(match)
    if doubtful_release(files, match, conflicts):
        report.add("conflicts", *conflicts)
        report.add("low_confidence_albums", {**low_confidence, "reason": "the card's numbers disagree with this release"})
        return
    report.add("conflicts", *apply_match(files, match))
    report.add("albums_matched", {"folder": folder, "tracks": len(files), **match.summary()})


async def prepare_albums(albums: dict[str, list[CardFile]], lookup: AlbumLookup | None,
                         report: ImportReport) -> tuple[list[CardFile], list[tuple[CardFile, CardFile]]]:
    """Dedupe each album folder and complete its numbering. Returns (kept files, dropped duplicates)."""
    kept_all, dropped_all = [], []
    for position, (folder, files) in enumerate(albums.items(), 1):
        kept, dropped = dedupe_album(files)
        infer_total_discs(kept)
        if lookup is not None:
            logger.info("[%d/%d] %s", position, len(albums), folder)
            await _lookup_album(lookup, folder, kept, report)
            infer_total_discs(kept)
        kept_all += kept
        dropped_all += dropped
    if lookup is not None:
        report.lookup_disabled = dict(lookup.disabled)
    return kept_all, dropped_all


async def _install(session: AsyncSession, plan: Planned) -> None:
    if plan.outcome == "enrich":
        plan.fills = await enrich_row(plan.row, plan.track)
        if not plan.fills:
            plan.outcome = "existing"
        return
    plan.track.file_size = await asyncio.to_thread(install_file, plan)
    if plan.outcome == "adopt":
        adopt_into(plan.row, plan.track)
        plan.track = plan.row
    else:
        session.add(plan.track)


async def install_planned(session: AsyncSession, plans: list[Planned], report: ImportReport, dry_run: bool) -> None:
    if dry_run:
        return
    written, failed = 0, {}
    for plan in plans:
        if plan.outcome == "existing" and id(plan.row) in failed:
            report.add("errors", {"card": plan.file.rel, "error": f"not imported: its other card copy "
                                                                  f"{failed[id(plan.row)]} failed; rerun the import"})
            plan.outcome = "error"
            continue
        if plan.outcome not in ("import", "adopt", "enrich"):
            continue
        try:
            await _install(session, plan)
        except Exception as e:
            logger.warning("Could not import %s: %s", plan.file.rel, e)
            report.add("errors", {"card": plan.file.rel, "error": str(e)[:200]})
            failed[id(plan.track)] = plan.file.rel
            plan.outcome = "error"
            continue
        written += 1
        if written % COMMIT_EVERY == 0:
            await session.commit()
    await session.commit()


def _replacement(plan: Planned) -> str | None:
    """The card path the next sync writes this song to, if it writes it at all."""
    if plan.outcome in ("import", "adopt", "enrich"):
        return plan.device_rel
    if plan.outcome == "existing" and plan.row is not None:
        return _row_device_rel(plan.row)
    return None


def _numbering_filled(f: CardFile) -> bool:
    """A number that decides the file's name on the card was filled (totals alone only change tags)."""
    return any(x.startswith(("track_number", "disc_number")) for x in f.filled) or (
        any(x.startswith("total_discs") for x in f.filled) and (f.total_discs or 0) > 1)


def record_outcomes(plans: list[Planned], dropped: list[tuple[CardFile, CardFile]], report: ImportReport) -> None:
    by_file = {id(p.file): p for p in plans}
    for plan in plans:
        f = plan.file
        entry = {"card": f.rel, "library": str(plan.dest), "card_path_after_sync": plan.device_rel}
        if plan.outcome in ("import", "adopt"):
            report.add("imported" if plan.outcome == "import" else "adopted", entry)
            if plan.retag:
                report.add("retagged", f.rel)
        elif plan.outcome == "enrich":
            report.add("enriched", {**entry, "library": plan.row.file_path, "track_id": plan.row.id, "fields": plan.fills})
        elif plan.outcome == "existing":
            report.add("skipped_existing", {**entry, "track_id": plan.row.id, "status": plan.row.status,
                                            "library_quality": plan.row.quality, "card_quality": plan.track.quality})
        elif plan.outcome == "queued":
            report.add("queued", {**entry, "track_id": plan.row.id, "status": plan.row.status})
        elif plan.outcome == "collision":
            report.add("collisions", {**entry, "owned_by_track": plan.row.id})
        if plan.outcome in ("import", "adopt") and _numbering_filled(f):
            report.add("numbers_filled", {"card": f.rel, "fields": f.filled})
        target = _replacement(plan)
        if target and card_key(target) != card_key(f.rel):
            report.add("stale_card_copies", {"card": f.rel, "replaced_by": target})
    for f, kept in dropped:
        report.add("duplicates", {"card": f.rel, "kept": kept.rel})
        target = _replacement(by_file[id(kept)]) if id(kept) in by_file else None
        if target and card_key(target) != card_key(f.rel):
            report.add("stale_card_copies", {"card": f.rel, "replaced_by": target, "reason": "duplicate"})


def _stands_for(plan: Planned) -> Track | None:
    if plan.outcome in ("import", "adopt"):
        return plan.track
    if plan.outcome in ("existing", "queued", "enrich"):
        return plan.row
    return None


def resolution_map(plans: list[Planned], dropped: list[tuple[CardFile, CardFile]]) -> dict[str, Track | None]:
    """Card path -> the library row that now stands for it (a dropped duplicate maps to the copy kept);
    None for a song this run failed to bring into the library."""
    resolved: dict[int, Track | None] = {}
    for plan in plans:
        resolved[id(plan.file)] = _stands_for(plan)
    out = {card_key(p.file.rel): resolved[id(p.file)] for p in plans}
    out.update({card_key(f.rel): resolved.get(id(kept)) for f, kept in dropped})
    return out


async def import_card(source: Path, report_path: Path, *, dry_run: bool = False, lookup: bool = True,
                      playlists: bool = True, limit_albums: int | None = None) -> ImportReport:
    """Import every song on the card into the library (see the module docstring); writes the report files."""
    files, read_errors = await asyncio.to_thread(scan_card, source)
    albums = group_by_album(files)
    if limit_albums:
        albums = dict(list(albums.items())[:limit_albums])
    report = ImportReport(str(source), dry_run, str(settings.library_dir))
    if playlists and limit_albums:
        report.playlists_skipped = ("a --limit-albums run sees only part of the card; the next sync would cut "
                                    "the card's playlists down to those albums")
    report.scanned = sum(len(v) for v in albums.values()) + len(read_errors)
    report.add("read_errors", *read_errors)
    album_lookup = AlbumLookup(report_path.parent / "lookup-cache.json") if lookup else None
    kept, dropped = await prepare_albums(albums, album_lookup, report)
    async with async_session() as session:
        index = LibraryIndex(list((await session.scalars(select(Track))).all()))
        plans = await asyncio.to_thread(lambda: [plan_file(f, index) for f in kept])
        await install_planned(session, plans, report, dry_run)
        record_outcomes(plans, dropped, report)
        if playlists and not limit_albums:
            await import_playlists(session, source, report_path.parent / "card-playlists",
                                   resolution_map(plans, dropped), report, dry_run)
    await asyncio.to_thread(write_report, report, report_path)
    return report


def write_report(report: ImportReport, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    report_path.with_suffix(".txt").write_text(report.summary() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Removing the stale card copies after a sync
# ---------------------------------------------------------------------------

def _on_card(root: Path, rel: str) -> Path | None:
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or ".." in parts:
        return None
    return root.joinpath(*parts)


def _keep_reason(root: Path, entry: dict, targets: dict[str, Track]) -> str | None:
    """Why a stale card copy must stay; None once its replacement is on the card, as big as the library file."""
    stale, replacement = _on_card(root, entry["card"]), _on_card(root, entry["replaced_by"])
    if stale is None or replacement is None or stale.suffix.lower() not in AUDIO_EXTS:
        return "not an audio path on the card"
    if card_key(entry["card"]) == card_key(entry["replaced_by"]) or card_key(entry["card"]) in targets:
        return "a library track syncs to this path"
    row = targets.get(card_key(entry["replaced_by"]))
    if row is None:
        return "no library track syncs to the replacement"
    library = Path(row.file_path)
    if not library.is_file():
        return "the replacement's library file is missing"
    if not replacement.is_file() or replacement.stat().st_size != library.stat().st_size:
        return "the replacement is not on the card yet; sync first"
    return None


def _stale_sidecars(stale: Path) -> list[Path]:
    # A sidecar named like another audio file in the folder (the kept copy of a duplicate, say) is that file's too.
    if any(stale.with_suffix(ext).exists() for ext in AUDIO_EXTS if ext != stale.suffix.lower()):
        return []
    return [stale.with_suffix(suffix) for suffix in SIDECAR_SUFFIXES if stale.with_suffix(suffix).is_file()]


def _remove_with_empty_parents(root: Path, paths: list[Path]) -> None:
    for path in paths:
        path.unlink()
    for parent in paths[0].parents:
        if parent == root or not parent.is_relative_to(root):
            break
        try:
            parent.rmdir()
        except OSError:
            break


def _playlists_naming(root: Path, removed: set[str]) -> list[str]:
    """Card playlists Leasure does not manage (the sync rewrites its own) that list a removed file."""
    managed = read_manifest(root)
    hits = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".m3u", PLAYLIST_EXT) or path.name in managed:
            continue
        try:
            entries = read_playlist(path)
        except (OSError, ValueError):
            continue
        if any(card_key(e) in removed for e in entries):
            hits.append(path.name)
    return hits


def remove_stale_copies(root: Path, entries: list[dict], rows: list[Track], apply: bool) -> dict:
    """Delete (or with apply=False only list) each stale card copy, with its sidecars, whose replacement
    the sync has written. Thread-side."""
    targets = {card_key(_row_device_rel(r)): r for r in rows if r.status == "done" and r.file_path}
    result = {"applied": apply, "removed": [], "kept": [], "already_gone": [], "errors": []}
    for entry in entries:
        reason = _keep_reason(root, entry, targets)
        if reason:
            result["kept"].append({**entry, "reason": reason})
            continue
        stale = _on_card(root, entry["card"])
        if not stale.is_file():
            result["already_gone"].append(entry["card"])
            continue
        sidecars = _stale_sidecars(stale)
        try:
            if apply:
                _remove_with_empty_parents(root, [stale, *sidecars])
        except OSError as e:
            result["errors"].append({"card": entry["card"], "error": str(e)[:200]})
            continue
        result["removed"].append({**entry, "sidecars": [p.name for p in sidecars]})
    result["playlists_naming_removed"] = _playlists_naming(root, {card_key(e["card"]) for e in result["removed"]})
    return result


async def clean_stale_card_copies(card: str, report_path: Path, *, apply: bool = False) -> dict:
    """Remove the stale_card_copies an import report lists from the card, once the sync has replaced them."""
    root = await asyncio.to_thread(resolve_sync_target, card)
    if not root:
        raise ValueError(f"{card} is not a detected removable drive; mount the card first")
    report = json.loads(await asyncio.to_thread(report_path.read_text, encoding="utf-8"))
    async with async_session() as session:
        rows = list((await session.scalars(select(Track).where(Track.status == "done"))).all())
    return await asyncio.to_thread(remove_stale_copies, Path(root), report.get("stale_card_copies", []), rows, apply)
