"""Qobuz FLAC downloads and FLAC upgrades of finished tracks.

Every Qobuz fetch passes the gate (session, breaker, cooldown, daily cap, track gap),
matches strictly, reserves a cap slot right before the stream body, then strips what
Qobuz shipped, decodes the whole file and tags it in staging before it reaches the library.
Upgrades swap files under a journal so a crash or a failed commit puts the old file back."""

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import async_session
from models import CardCopy, SupersededFile, Track, utc_now
from services import qobuz_client, qobuz_session, secret_file, tagger, track_locks, yt_engine
from services.device import build_device_path
from services.qobuz_client import FileUrl, QobuzClient, QobuzTrack
from services.yt_engine import CapDeferred, DownloadFailed, with_audio_suffix

logger = logging.getLogger(__name__)

DAILY_FILE = settings.data_dir / "qobuz_daily.json"
UPGRADES_DIR = settings.data_dir / "upgrades"
MAX_SAMPLING_KHZ = 192.0
TRACK_GAP_S: tuple[float, float] = (3.0, 8.0)
COOLDOWN_DEFAULT_S = 300
BREAKER_OPEN_S = 3600
CDN_FAILURES_TO_OPEN = 3
MIN_MATCH_SCORE_DOWNLOAD = 4.0
MIN_MATCH_SCORE_UPGRADE = 5.0
UPGRADE_LENGTH_TOLERANCE_S = 3.0
SKIP_REASONS = ("not_configured", "signed_out", "ineligible", "cap", "no_match",
                "not_streamable", "unavailable", "cdn_unavailable")

DISK_FULL_RETRY_S = 900
_GIB = 1024**3
_HEAD_BYTES = 1024 * 1024
_MAX_CANDIDATES = 3
_ID_MATCH_SCORE = 6.0

_SKIP_MESSAGES = {
    "not_configured": "Qobuz is not signed in — sign in from the Qobuz card on Home, or pick Best/MP3.",
    "signed_out": "Qobuz sign-in expired — sign in again from the Qobuz card on Home.",
    "ineligible": "This Qobuz plan has no lossless streaming.",
    "cap": "Daily Qobuz cap reached.",
    "no_match": "No confident match on Qobuz.",
    "not_streamable": "Qobuz cannot stream this track in lossless (preview only / not available in your region).",
    "unavailable": "Qobuz changed its web player or refused the app — Leasure paused Qobuz for an hour.",
    "cdn_unavailable": "Qobuz downloads kept failing — Leasure paused Qobuz for an hour.",
}
COOLDOWN_MESSAGE = "Qobuz asked Leasure to slow down — this track stays queued and resumes in a few minutes."
BREAKER_MESSAGE = "Qobuz is unavailable right now — this FLAC track stays queued and is retried in about an hour."
DISK_MESSAGE = "Not enough free disk space for this FLAC — this track stays queued."
UPGRADE_NO_MATCH = "No exact Qobuz match; the current file is kept."
DAMAGED_MESSAGE = "The Qobuz download is damaged or incomplete — retry."
NOT_REVERTIBLE = "There is no earlier file to go back to."

BENIGN_VERSION = re.compile(r"\b(?:\d{4} remaster(?:ed)?(?: version)?|remaster(?:ed)?(?: \d{4})?(?: version)?"
                             r"|album version|explicit|single version|original mix)\b")
_NUMBER_TOKEN = re.compile(r"\b(?:\d+|i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)\b")
_ARTIST_SPLIT = re.compile(r",|&|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b")
_EXTRA_VARIANTS = ("acoustic", "demo", "edit", "radio edit", "extended", "re-recorded", "rerecorded",
                   "taylor's version", "mono", "a cappella", "acapella", "orchestral", "karaoke", "live")
_LAYOUT_FIELDS = ("artist", "title", "album", "album_artist", "track_number", "disc_number",
                  "total_tracks", "total_discs")
_PROVENANCE_FIELDS = ("engine_used", "format", "quality", "source_format_id", "source_codec", "source_bitrate_kbps",
                      "source_sample_rate", "source_bit_depth", "premium_used", "transcoded", "is_lossless",
                      "verification", "matched_title", "match_score", "qobuz_id", "qobuz_isrc", "qobuz_match_method",
                      "isrc", "artwork_url", "file_size", "qobuz_skip_reason", "file_path", "downloaded_at")
_SIDECARS = (".jpg", ".lrc")


class QobuzSkip(Exception):
    """Qobuz was not used for this track; the caller may fall back. str() is the user-facing sentence."""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or _SKIP_MESSAGES.get(reason, "Qobuz was not used for this track."))
        self.reason = reason


class _Damaged(DownloadFailed):
    """The delivered body is not a whole, playable FLAC; counts toward the CDN breaker."""


class _RateTooHigh(DownloadFailed):
    """The FLAC is above 192 kHz; the caller asks again at format 7."""


@dataclass(frozen=True)
class TrackSnapshot:
    id: int
    title: str
    artist: str
    album: str | None
    album_artist: str | None
    duration_ms: int
    isrc: str | None
    qobuz_id: str | None
    qobuz_match_method: str | None
    artwork_url: str | None
    file_path: str | None
    quality: str


@dataclass(frozen=True)
class QobuzMatch:
    track: QobuzTrack
    score: float
    method: str


@dataclass(frozen=True)
class FlacFacts:
    bits: int
    sample_rate: int
    channels: int
    length_s: float
    decoded_s: float
    verification: str


_daily_lock = threading.Lock()
_breaker: dict = {"reason": None, "until": 0.0}
_cdn_failures = 0
_gap_lock = asyncio.Lock()
_last_fetch_end = 0.0


def _reset_for_tests() -> None:
    global _cdn_failures, _gap_lock, _last_fetch_end
    _breaker.update(reason=None, until=0.0)
    _cdn_failures = 0
    _gap_lock = asyncio.Lock()
    _last_fetch_end = 0.0


def _who(row: TrackSnapshot) -> str:
    return f"'{row.artist} - {row.title}'"


# --- daily counter, cooldown and breaker -------------------------------------------------

def _read_daily() -> dict:
    try:
        state = json.loads(DAILY_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _today_count(state: dict) -> int:
    return int(state.get("count", 0)) if state.get("date") == yt_engine.counter_date() else 0


def downloads_today() -> int:
    return _today_count(_read_daily())


def daily_cap_reached() -> bool:
    return downloads_today() >= settings.qobuz_max_downloads_per_day


def reserve_daily_slot() -> bool:
    """Count one Qobuz stream against today's cap; False when the cap is used up."""
    with _daily_lock:
        state = _read_daily()
        count = _today_count(state)
        if count >= settings.qobuz_max_downloads_per_day:
            return False
        state.update(date=yt_engine.counter_date(), count=count + 1)
        secret_file.write_atomic(DAILY_FILE, json.dumps(state))
        return True


def set_cooldown(seconds: float) -> None:
    with _daily_lock:
        state = _read_daily()
        state.update(date=yt_engine.counter_date(), count=_today_count(state),
                     cooldown_until=(datetime.now(UTC) + timedelta(seconds=seconds)).isoformat())
        secret_file.write_atomic(DAILY_FILE, json.dumps(state))


def cooldown_left() -> float:
    until = _read_daily().get("cooldown_until")
    try:
        moment = datetime.fromisoformat(until) if until else None
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (moment - datetime.now(UTC)).total_seconds()) if moment else 0.0


def _open_breaker(reason: str) -> None:
    _breaker.update(reason=reason, until=time.time() + BREAKER_OPEN_S)
    logger.warning("Qobuz paused for %d s: %s", BREAKER_OPEN_S, reason)


def _breaker_left() -> tuple[str | None, float]:
    left = _breaker["until"] - time.time()
    return (_breaker["reason"], left) if _breaker["reason"] and left > 0 else (None, 0.0)


def breaker_state() -> dict:
    reason, _ = _breaker_left()
    until = datetime.fromtimestamp(_breaker["until"], UTC).isoformat(timespec="seconds") if reason else None
    return {"reason": reason, "until": until}


def _note_cdn_failure() -> None:
    global _cdn_failures
    _cdn_failures += 1
    if _cdn_failures >= CDN_FAILURES_TO_OPEN:
        _cdn_failures = 0
        _open_breaker("cdn_unavailable")


def quality_tier(bits: int, sample_rate_hz: int) -> str:
    return f"flac_{bits}_{sample_rate_hz // 1000}"


# --- gate ---------------------------------------------------------------------------------

def _skip_or_defer(route: str, reason: str, deferral: str, retry_after_s: float) -> Exception:
    """`native` falls back to YouTube; FLAC-only rows and upgrades wait instead."""
    if route == "native":
        return QobuzSkip(reason)
    return CapDeferred(deferral, retry_after_s=retry_after_s)


def _cap_exception(route: str) -> Exception:
    used = downloads_today()
    if route == "native":
        return QobuzSkip("cap", f"Daily Qobuz cap reached ({used} today).")
    return CapDeferred(f"Daily Qobuz cap reached ({used} today) — this FLAC track stays queued and resumes tomorrow.",
                       retry_after_s=yt_engine.seconds_until_tomorrow())


async def _wait_track_gap() -> None:
    global _last_fetch_end
    async with _gap_lock:
        if _last_fetch_end:
            wait = _last_fetch_end + random.uniform(*TRACK_GAP_S) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        # Marks the start too, so a concurrent worker waits even before this fetch ends.
        _last_fetch_end = time.monotonic()


def _fetch_ended() -> None:
    global _last_fetch_end
    _last_fetch_end = time.monotonic()


async def _gate(route: str) -> qobuz_session.StoredSession:
    session = await asyncio.to_thread(qobuz_session.load)
    if session is None:
        raise QobuzSkip("not_configured")
    if session.state != "connected":
        raise QobuzSkip("ineligible" if session.state == "ineligible" else "signed_out")
    reason, left = _breaker_left()
    if reason:
        raise _skip_or_defer(route, reason, BREAKER_MESSAGE, left)
    cooling = await asyncio.to_thread(cooldown_left)
    if cooling > 0:
        raise CapDeferred(COOLDOWN_MESSAGE, retry_after_s=cooling)
    if await asyncio.to_thread(daily_cap_reached):
        raise _cap_exception(route)
    await _wait_track_gap()
    return session


def _before_body_hook(route: str, expected_s: float | None):
    """Thread-side, right before the CDN body is read: room on disk, then one cap slot."""
    def hook(_stream: FileUrl, content_length: int | None) -> None:
        size = content_length or int((expected_s or 600) * 192000 * 6)
        needed = 2 * size + _GIB
        for folder in (settings.download_dir, settings.library_dir):
            if shutil.disk_usage(folder).free < needed:
                raise CapDeferred(DISK_MESSAGE, retry_after_s=DISK_FULL_RETRY_S)
        if not reserve_daily_slot():
            raise _cap_exception(route)
    return hook


# --- matching -----------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    lowered = BENIGN_VERSION.sub(" ", (text or "").casefold())
    return " ".join(re.sub(r"[\W_]+", " ", lowered).split())


_VARIANT_WORDS = tuple(dict.fromkeys(_norm(w) for w in (*yt_engine._VARIANT_WORDS, *_EXTRA_VARIANTS)))


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _has_words(text: str, words: str) -> bool:
    return f" {words} " in f" {text} "


def _primary_artist(artist: str) -> str:
    return _norm(_ARTIST_SPLIT.split(artist.casefold())[0])


def _passes_gates(row: TrackSnapshot, track: QobuzTrack) -> tuple[bool, str, float]:
    """(all hard gates pass, cleaned title used for scoring, title ratio)."""
    wanted = _norm(row.title)
    base, full = _norm(track.title), _norm(track.full_title)
    title_ratio, cleaned = max((_ratio(wanted, base), base), (_ratio(wanted, full), full))
    primary = _primary_artist(row.artist)
    artist_ok = bool(primary) and any(_has_words(_norm(name), primary) for name in (track.performer, track.album_artist))
    duration_ok = not (row.duration_ms and track.duration_s) or abs(track.duration_s - row.duration_ms / 1000) <= 10
    numbers_ok = set(_NUMBER_TOKEN.findall(wanted)) == set(_NUMBER_TOKEN.findall(full))
    variants_ok = not any(_has_words(full, w) and not _has_words(wanted, w) for w in _VARIANT_WORDS)
    ok = (track.streamable is not False and title_ratio >= 0.9 and artist_ok and duration_ok
          and numbers_ok and variants_ok)
    return ok, cleaned, title_ratio


def _album_ratio(row: TrackSnapshot, track: QobuzTrack) -> float:
    return _ratio(_norm(row.album), _norm(track.album_title)) if row.album else 1.0


def _candidates(client: QobuzClient, row: TrackSnapshot) -> list[tuple[QobuzTrack, str]]:
    if row.qobuz_id and row.qobuz_match_method in ("id", "isrc"):
        with contextlib.suppress(qobuz_client.QobuzNotFoundError):
            return [(client.get_track(row.qobuz_id), "id")]
    if row.isrc:
        hits = client.find_by_isrc(row.isrc)
        if hits:
            return [(t, "isrc") for t in hits]
    found: list[tuple[QobuzTrack, str]] = []
    if row.qobuz_id:
        with contextlib.suppress(qobuz_client.QobuzNotFoundError):
            found.append((client.get_track(row.qobuz_id), "search"))
    query = f"{row.artist.split(',')[0].strip()} {row.title}"
    found += [(t, "search") for t in client.search_tracks(query, limit=10)]
    seen: set[str] = set()
    return [(t, m) for t, m in found if not (t.id in seen or seen.add(t.id))]


def resolve_matches(client: QobuzClient, row: TrackSnapshot, *, strict: bool) -> list[QobuzMatch]:
    """Up to three candidates that pass every gate, best first; QobuzSkip("no_match") when none does."""
    threshold = MIN_MATCH_SCORE_UPGRADE if strict else MIN_MATCH_SCORE_DOWNLOAD
    ranked: list[tuple[float, bool, QobuzMatch]] = []
    for track, method in _candidates(client, row):
        ok, cleaned, _ = _passes_gates(row, track)
        if not ok:
            continue
        album = _album_ratio(row, track)
        if method == "search":
            if strict and album < 0.6:
                continue
            score = yt_engine.score_candidate(
                yt_engine.Candidate(video_id=track.id, title=cleaned, artist=track.performer, duration_s=track.duration_s),
                _norm(row.title), row.artist, row.duration_ms)
            if score is None or score < threshold:
                continue
        else:
            score = _ID_MATCH_SCORE
        ranked.append((score + 0.5 * album, bool(track.hires_streamable), QobuzMatch(track, score, method)))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        raise QobuzSkip("no_match", UPGRADE_NO_MATCH if strict else f"No confident match on Qobuz for {_who(row)}.")
    return [match for _, _, match in ranked[:_MAX_CANDIDATES]]


# --- the file -----------------------------------------------------------------------------

def strip_shipped_tags(path: Path) -> None:
    """Drop the tags and pictures Qobuz shipped (and any ID3 block) before Leasure's tagger runs."""
    from mutagen.flac import FLAC

    try:
        audio = FLAC(path)
        audio.delete()
        audio.clear_pictures()
        audio.save(deleteid3=True, padding=lambda _info: 262144)
    except Exception:
        raise _Damaged("Qobuz sent a file that is not FLAC — retry later.") from None


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "ffmpeg"
    if local.exists():
        return str(local)
    raise DownloadFailed("ffmpeg is required to check FLAC downloads — install it.")


def _decoded_seconds(path: Path) -> float:
    """Decode the whole file; any CRC or frame error, or a short decode, means damaged."""
    args = [_ffmpeg(), "-nostdin", "-v", "error", "-err_detect", "crccheck+explode", "-i", str(path),
            "-map", "0:a:0", "-f", "null", "-progress", "pipe:1", "-"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        raise _Damaged(DAMAGED_MESSAGE) from None
    times = re.findall(r"^out_time_us=(\d+)\s*$", proc.stdout, re.MULTILINE)
    if proc.returncode != 0 or proc.stderr.strip() or not times:
        raise _Damaged(DAMAGED_MESSAGE)
    return int(times[-1]) / 1e6


def verify_flac(path: Path, duration_ms: int, expected_duration_s: float | None) -> FlacFacts:
    """STREAMINFO plus a full decode decide what was delivered. Raises DownloadFailed."""
    from mutagen.flac import FLAC

    try:
        info = FLAC(path).info
    except Exception:
        raise _Damaged("Qobuz sent a file that is not FLAC — retry later.") from None
    if info.channels not in (1, 2) or info.bits_per_sample not in (16, 24):
        raise DownloadFailed(f"Qobuz sent a FLAC the H2 cannot play ({info.channels} channels, "
                             f"{info.bits_per_sample}-bit).")
    if info.sample_rate > MAX_SAMPLING_KHZ * 1000:
        raise _RateTooHigh(f"Qobuz sent {info.sample_rate / 1000:g} kHz audio, above what the H2 plays.")
    track_s = expected_duration_s or duration_ms / 1000
    if track_s > 60 and info.length <= 35:
        raise _Damaged("Qobuz served a 30-second preview, not the full track.")
    decoded = _decoded_seconds(path)
    if abs(decoded - info.length) > 0.5:
        raise _Damaged(DAMAGED_MESSAGE)
    if not info.length or path.stat().st_size * 8 / info.length < 128000 * info.channels:
        raise _Damaged(DAMAGED_MESSAGE)
    return FlacFacts(bits=info.bits_per_sample, sample_rate=info.sample_rate, channels=info.channels,
                     length_s=info.length, decoded_s=decoded,
                     verification=yt_engine._verify_duration(info.length, duration_ms))


def _download_one(client: QobuzClient, match: QobuzMatch, dest: Path, hook, row: TrackSnapshot
                  ) -> tuple[FlacFacts, FileUrl]:
    expected = float(match.track.duration_s) if match.track.duration_s else (row.duration_ms / 1000 or None)
    max_format = settings.qobuz_max_format
    while True:
        got = client.download(match.track.id, dest, max_format_id=max_format, max_sampling_khz=MAX_SAMPLING_KHZ,
                              expected_duration_s=expected, before_body=hook)
        strip_shipped_tags(dest)
        try:
            return verify_flac(dest, row.duration_ms, expected), got.stream
        except _RateTooHigh:
            if max_format != qobuz_client.FORMAT_FLAC_24_192:
                raise
            max_format = qobuz_client.FORMAT_FLAC_24_96


def _fetch_verified(client: QobuzClient, row: TrackSnapshot, staging: Path, hook, strict: bool
                    ) -> tuple[QobuzMatch, FlacFacts, FileUrl]:
    """Thread-side: match, download and verify into staging/source.flac."""
    matches = resolve_matches(client, row, strict=strict)
    staging.mkdir(parents=True, exist_ok=True)
    for match in matches:
        try:
            return (match, *_download_one(client, match, staging / "source.flac", hook, row))
        except qobuz_client.QobuzNotStreamableError as e:
            logger.info("Qobuz track %s not streamable (%s); trying the next candidate", match.track.id, e.reason)
    if strict:
        raise QobuzSkip("not_streamable")
    raise QobuzSkip("not_streamable", f"Qobuz cannot stream {_who(row)} in lossless "
                                      "(preview only / not available in your region).")


async def _call_qobuz(route: str, fingerprint: str, fn, *args):
    """Run a blocking Qobuz step and turn client errors into skip / deferral / failure."""
    try:
        return await asyncio.to_thread(fn, *args)
    except _Damaged:
        _note_cdn_failure()
        raise
    except (QobuzSkip, CapDeferred, DownloadFailed):
        raise
    except qobuz_session.SessionChanged:
        raise QobuzSkip("signed_out") from None
    except qobuz_client.QobuzAuthError:
        await asyncio.to_thread(qobuz_session.mark_expired, qobuz_session.EXPIRED_MESSAGE, fingerprint)
        raise QobuzSkip("signed_out") from None
    except qobuz_client.QobuzRateLimitError as e:
        wait = e.retry_after or COOLDOWN_DEFAULT_S
        await asyncio.to_thread(set_cooldown, wait)
        raise CapDeferred(COOLDOWN_MESSAGE, retry_after_s=wait) from None
    except qobuz_client.QobuzNotStreamableError:
        raise QobuzSkip("not_streamable") from None
    except (qobuz_client.QobuzAppError, qobuz_client.QobuzForbiddenError) as e:
        await asyncio.to_thread(qobuz_session.note_error, qobuz_client.redact(str(e)), fingerprint)
        _open_breaker("unavailable")
        raise _skip_or_defer(route, "unavailable", BREAKER_MESSAGE, BREAKER_OPEN_S) from None
    except (qobuz_client.QobuzDownloadError, qobuz_client.QobuzNetworkError) as e:
        _note_cdn_failure()
        raise DownloadFailed(qobuz_client.redact(str(e)) or "The Qobuz download failed — retry.") from None
    except qobuz_client.QobuzError as e:
        raise DownloadFailed(qobuz_client.redact(str(e)) or "Qobuz did not answer as expected.") from None


async def _run_fetch(route: str, session: qobuz_session.StoredSession, row: TrackSnapshot, staging: Path,
                     strict: bool) -> tuple[QobuzMatch, FlacFacts, FileUrl]:
    global _cdn_failures
    fingerprint = qobuz_session.token_fingerprint(session.user_auth_token)
    client = await _call_qobuz(route, fingerprint, qobuz_session.make_client, session)
    expected = row.duration_ms / 1000 or None
    try:
        fetched = await _call_qobuz(route, fingerprint, _fetch_verified, client, row, staging,
                                    _before_body_hook(route, expected), strict)
        _cdn_failures = 0
        return fetched
    finally:
        if client.rotated_token:
            await asyncio.to_thread(qobuz_session.store_rotated_token, fingerprint, client.rotated_token)
        await asyncio.to_thread(client.close)


def _cover_override(row: TrackSnapshot, track: QobuzTrack) -> str | None:
    """The Qobuz cover replaces nothing but an empty URL or a YouTube thumbnail of the same album."""
    if not track.cover_url:
        return None
    if not row.artwork_url:
        return track.cover_url
    youtube_thumb = urlsplit(row.artwork_url).hostname == "i.ytimg.com"
    if youtube_thumb and (not row.album or _ratio(_norm(row.album), _norm(track.album_title)) >= 0.6):
        return track.cover_url
    return None


async def _tag_staged(staged: Path, track_id: int, override: str | None) -> None:
    try:
        await tagger.apply_full_metadata(staged, track_id, artwork_url_override=override)
    except Exception as e:
        logger.exception("Tagging the Qobuz download of track %d failed", track_id)
        first_line = qobuz_client.redact(str(e)).splitlines()[0][:200] if str(e) else e.__class__.__name__
        raise DownloadFailed(f"Downloaded from Qobuz, but tagging failed: {first_line}") from None


def _provenance(row: TrackSnapshot, match: QobuzMatch, facts: FlacFacts, stream: FileUrl,
                override: str | None, size: int) -> dict:
    values = {
        "engine_used": "qobuz", "format": "flac", "quality": quality_tier(facts.bits, facts.sample_rate),
        "source_format_id": str(stream.format_id), "source_codec": "flac", "source_bitrate_kbps": None,
        "source_sample_rate": facts.sample_rate, "source_bit_depth": facts.bits, "premium_used": None,
        "transcoded": False, "is_lossless": True, "verification": facts.verification,
        "matched_title": match.track.full_title, "match_score": match.score, "qobuz_id": str(match.track.id),
        "qobuz_isrc": match.track.isrc, "qobuz_match_method": match.method, "file_size": size,
        "qobuz_skip_reason": None,
    }
    if not row.isrc and match.method in ("id", "isrc") and match.track.isrc:
        values["isrc"] = match.track.isrc
    if override:
        values["artwork_url"] = override
    return values


async def _snapshot(track_id: int) -> TrackSnapshot:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            raise DownloadFailed("This track is no longer in the library.")
        return TrackSnapshot(id=track.id, title=track.title or "", artist=track.artist or "", album=track.album,
                             album_artist=track.album_artist, duration_ms=track.duration_ms or 0, isrc=track.isrc,
                             qobuz_id=track.qobuz_id, qobuz_match_method=track.qobuz_match_method,
                             artwork_url=track.artwork_url, file_path=track.file_path, quality=track.quality)


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    yt_engine._move_into_library(src, dest)


def _move_staged(staged: Path, dest: Path) -> None:
    _move(staged, dest)
    for suffix in _SIDECARS:
        sidecar = staged.with_suffix(suffix)
        if sidecar.exists():
            _move(sidecar, dest.with_suffix(suffix))


# --- download -----------------------------------------------------------------------------

async def qobuz_download(track_id: int) -> Path:
    """Download a pending track's FLAC from Qobuz into the library. QobuzSkip | CapDeferred | DownloadFailed."""
    row = await _snapshot(track_id)
    route = "flac_lossless" if row.quality == "flac_lossless" else "native"
    session = await _gate(route)
    staging = settings.download_dir / str(track_id)
    try:
        match, facts, stream = await _run_fetch(route, session, row, staging, strict=False)
        staged = staging / "source.flac"
        override = _cover_override(row, match.track)
        await _tag_staged(staged, track_id, override)
        async with track_locks.hold(track_id):
            async with async_session() as db_session:
                track = await db_session.get(Track, track_id)
                if not track:
                    raise DownloadFailed("This track is no longer in the library.")
                stem = yt_engine._track_stem(track)
            dest = with_audio_suffix(stem, ".flac")
            taken = await yt_engine._files_of_other_tracks(track_id, stem)
            if await asyncio.to_thread(_stem_owned_elsewhere, taken, None):
                raise DownloadFailed(f"Another track in the library is already saved as '{dest.stem}' in "
                                     f"'{dest.parent.name}'. Remove the duplicate, then retry.")
            size = (await asyncio.to_thread(staged.stat)).st_size
            await asyncio.to_thread(_move_staged, staged, dest)
            values = _provenance(row, match, facts, stream, override, size)
            async with async_session() as db_session:
                track = await db_session.get(Track, track_id)
                for name, value in values.items():
                    setattr(track, name, value)
                await db_session.commit()
    finally:
        _fetch_ended()
        await asyncio.to_thread(shutil.rmtree, staging, True)
    logger.info("Track %d downloaded from Qobuz (%s, format %s)", track_id, values["quality"], stream.format_id)
    return dest


# --- upgrades -----------------------------------------------------------------------------

def _head_sha256(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read(_HEAD_BYTES)).hexdigest()


def _file_identity(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime, "head": _head_sha256(path)}


def _path_key(path: str | Path) -> str:
    from services.card_import import _path_key as key

    return key(path)


def _journal_dir(track_id: int) -> Path:
    return UPGRADES_DIR / str(track_id)


def _read_journal(track_id: int) -> dict | None:
    try:
        journal = json.loads((_journal_dir(track_id) / "journal.json").read_text())
    except (OSError, ValueError):
        return None
    return journal if isinstance(journal, dict) else None


def _write_journal(journal: dict) -> None:
    folder = _journal_dir(journal["track_id"])
    folder.mkdir(parents=True, exist_ok=True)
    secret_file.write_atomic(folder / "journal.json", json.dumps(journal, indent=2))


def _old_file_facts(old: Path) -> float:
    """Length of the current library file, after checking it is a real file inside the library."""
    import mutagen

    if old.is_symlink() or not old.is_file():
        raise DownloadFailed("The current file is missing from the library; download the track again instead.")
    if not old.resolve().is_relative_to(settings.library_dir.resolve()):
        raise DownloadFailed("The current file is outside the library folder; move the library first "
                             "(scripts/move_library.py).")
    try:
        audio = mutagen.File(old)
        length = float(audio.info.length) if audio is not None else 0.0
    except Exception:
        length = 0.0
    if not length:
        raise DownloadFailed("The current file cannot be read, so its length cannot be compared with Qobuz.")
    return length


async def _is_shared(track_id: int, old: Path) -> bool:
    async with async_session() as session:
        paths = await session.scalars(select(Track.file_path).where(Track.id != track_id, Track.file_path.is_not(None)))
        key = _path_key(old)
        return any(_path_key(p) == key for p in paths)


def _stem_owned_elsewhere(taken: frozenset[str], own: Path | None) -> bool:
    """Thread-side: another row's file exists at this stem (any extension), so the .jpg/.lrc there are its too."""
    own_key = _path_key(own) if own else None
    return any(_path_key(p) != own_key and Path(p).exists() for p in taken)


def _same_file(a: Path, b: Path) -> bool:
    if a.exists() and b.exists():
        return a.samefile(b)
    return _path_key(a) == _path_key(b)


def _swap_in(track_id: int, old: Path, staged: Path, dest: Path, shared: bool, old_values: dict) -> dict:
    """Thread-side: old audio and sidecars into data/upgrades/<id>/old/, staged FLAC into place.
    Every step is journaled before it happens, so _rollback_swap can always undo it."""
    backup = _journal_dir(track_id) / "old"
    backup.mkdir(parents=True, exist_ok=True)
    journal = {"track_id": track_id, "phase": "swapping", "old": str(old), "new": str(dest),
               "provenance": old_values, "old_identity": _file_identity(old), "moved": [], "created": []}
    _write_journal(journal)
    keys: set[str] = set()
    targets = [old, *(old.with_suffix(s) for s in _SIDECARS), *(dest.with_suffix(s) for s in _SIDECARS)]
    for index, path in enumerate(targets):
        if not path.exists() or _path_key(path) in keys:
            continue
        keys.add(_path_key(path))
        kept = backup / f"{index:02d}_{path.name}"
        # A file another row also points at stays where it is; only a copy is kept.
        copy = shared and index <= len(_SIDECARS)
        journal["moved"].append({"from": str(path), "to": str(kept), "copied": copy})
        _write_journal(journal)
        if copy:
            shutil.copyfile(path, kept)
        else:
            _move(path, kept)
    installs = [(staged, dest)] + [(staged.with_suffix(s), dest.with_suffix(s)) for s in _SIDECARS
                                   if staged.with_suffix(s).exists()]
    for src, target in installs:
        journal["created"].append(str(target))
        _write_journal(journal)
        _move(src, target)
    # A sidecar the tagger could not make this time (no lyrics, a failed cover fetch) keeps the old one.
    for suffix in _SIDECARS:
        kept = next((Path(m["to"]) for m in journal["moved"] if m["from"] == str(old.with_suffix(suffix))), None)
        target = dest.with_suffix(suffix)
        if kept and not target.exists():
            journal["created"].append(str(target))
            _write_journal(journal)
            shutil.copyfile(kept, target)
    return journal


def _rollback_swap(journal: dict) -> None:
    """Undo a swap from its journal: remove what was installed, move the backups back."""
    for created in reversed(journal["created"]):
        Path(created).unlink(missing_ok=True)
    for moved in reversed(journal["moved"]):
        source, kept = Path(moved["from"]), Path(moved["to"])
        if kept.exists() and not source.exists():
            _move(kept, source)
    shutil.rmtree(_journal_dir(journal["track_id"]), ignore_errors=True)


def _jsonable(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _card_rel(track: Track, ext: str) -> str:
    return build_device_path(track.artist, track.album, track.track_number, track.title, ext,
                             album_artist=track.album_artist, disc_number=track.disc_number,
                             total_discs=track.total_discs)


async def _superseded_rows(track: Track, old_rel: str, new_rel: str, identity: dict, reason: str) -> list[SupersededFile]:
    """What the next sync may remove from cards: this track's recorded copies of the replaced
    file (and sidecars that move), plus a byte identity of the replaced library file for any card."""
    if old_rel.casefold() == new_rel.casefold():
        return []
    new_stem = new_rel.rsplit(".", 1)[0]
    keep = {f"{new_stem}{s}".casefold() for s in (*_SIDECARS, new_rel[len(new_stem):])}
    async with async_session() as session:
        copies = (await session.scalars(select(CardCopy).where(CardCopy.track_id == track.id))).all()
    rows = [SupersededFile(track_id=track.id, card_id=c.card_id, basis="card_copy", reason=reason, kind=c.kind,
                           rel_path=c.rel_path, size=c.size, mtime=c.mtime, head_sha256=c.head_sha256)
            for c in copies if c.rel_key not in keep]
    rows.append(SupersededFile(track_id=track.id, card_id=None, basis="library", reason=reason, kind="audio",
                               rel_path=old_rel, size=identity["size"], mtime=identity["mtime"],
                               head_sha256=identity["head"]))
    return rows


async def _compare_and_set(track_id: int, expected: dict, values: dict, superseded: list[SupersededFile]) -> bool:
    """One transaction: the row update only if nothing moved under us, plus the superseded records."""
    conditions = [Track.id == track_id, *[getattr(Track, name).is_not_distinct_from(value)
                                          for name, value in expected.items()]]
    async with async_session() as session:
        result = await session.execute(update(Track).where(*conditions).values(**values)
                                       .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await session.rollback()
            return False
        session.add_all(superseded)
        await session.commit()
        return True


async def _set_upgrade_queued(track_id: int) -> None:
    async with async_session() as session:
        await session.execute(update(Track).where(Track.id == track_id).values(upgrade_state="queued")
                              .execution_options(synchronize_session=False))
        await session.commit()


_REQUEUED = "The track changed while its FLAC upgrade ran — it is queued again."


async def _swap_and_commit(track_id: int, old: Path, staged: Path, shared: bool,
                           row: TrackSnapshot, match: QobuzMatch, facts: FlacFacts, stream: FileUrl,
                           override: str | None) -> Path:
    """Under the track lock: move the files, then commit only if the row is still what we upgraded."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
    if (not track or track.status != "done" or track.file_path != str(old)
            or track.upgrade_state != "downloading"):
        await _set_upgrade_queued(track_id)
        raise CapDeferred(_REQUEUED, retry_after_s=60)
    dest = with_audio_suffix(yt_engine._track_stem(track), ".flac")
    taken = await yt_engine._files_of_other_tracks(track_id, yt_engine._track_stem(track))
    if ((str(dest) in taken or dest.exists()) and not await asyncio.to_thread(_same_file, dest, old)) or \
            await asyncio.to_thread(_stem_owned_elsewhere, taken, old):
        raise DownloadFailed(f"Another file is already saved as '{dest.stem}' in '{dest.parent.name}'; "
                             "the current file is kept.")
    if shared and _path_key(dest) == _path_key(old):
        raise DownloadFailed("Another track shares this file, so it cannot be replaced; the current file is kept.")
    old_values = {name: _jsonable(getattr(track, name)) for name in _PROVENANCE_FIELDS}
    size = (await asyncio.to_thread(staged.stat)).st_size
    until = utc_now() + timedelta(days=settings.qobuz_upgrade_keep_days)
    values = {**_provenance(row, match, facts, stream, override, size), "file_path": str(dest),
              "downloaded_at": utc_now(), "synced_at": None, "upgrade_state": None, "upgrade_error": None,
              "upgrade_backup_until": until}
    expected = {"file_path": str(old), "upgrade_state": "downloading",
                **{name: getattr(track, name) for name in _LAYOUT_FIELDS}}
    try:
        journal = await asyncio.to_thread(_swap_in, track_id, old, staged, dest, shared, old_values)
        old_rel = _card_rel(track, old.suffix.lstrip("."))
        superseded = [] if shared else await _superseded_rows(track, old_rel, _card_rel(track, "flac"),
                                                              journal["old_identity"], "upgrade")
        committed = await _compare_and_set(track_id, expected, values, superseded)
    except Exception:
        journal = await asyncio.to_thread(_read_journal, track_id)
        if journal and journal.get("phase") == "swapping":
            await asyncio.to_thread(_rollback_swap, journal)
        logger.exception("Committing the FLAC upgrade of track %d failed", track_id)
        raise DownloadFailed("Could not save the FLAC upgrade; the previous file is back.") from None
    if not committed:
        await asyncio.to_thread(_rollback_swap, journal)
        await _set_upgrade_queued(track_id)
        raise CapDeferred(_REQUEUED, retry_after_s=60)
    journal.update(phase="committed", backup_until=until.isoformat())
    await asyncio.to_thread(_write_journal, journal)
    return dest


async def qobuz_upgrade(track_id: int) -> Path:
    """Replace a finished track's file with its Qobuz FLAC. The old file is kept for Revert."""
    row = await _snapshot(track_id)
    if not row.file_path:
        raise DownloadFailed("This track has no library file to upgrade.")
    old = Path(row.file_path)
    old_length = await asyncio.to_thread(_old_file_facts, old)
    shared = await _is_shared(track_id, old)
    session = await _gate("upgrade")
    staging = settings.download_dir / str(track_id)
    try:
        match, facts, stream = await _run_fetch("upgrade", session, row, staging, strict=True)
        if abs(facts.length_s - old_length) > UPGRADE_LENGTH_TOLERANCE_S:
            raise QobuzSkip("no_match", UPGRADE_NO_MATCH)
        staged = staging / "source.flac"
        override = _cover_override(row, match.track)
        await _tag_staged(staged, track_id, override)
        async with track_locks.hold(track_id):
            dest = await _swap_and_commit(track_id, old, staged, shared, row, match, facts, stream, override)
    finally:
        _fetch_ended()
        await asyncio.to_thread(shutil.rmtree, staging, True)
    logger.info("Track %d upgraded to %s (%s)", track_id, dest.name, quality_tier(facts.bits, facts.sample_rate))
    return dest


# --- revert, recovery, retention ----------------------------------------------------------

def _restore_values(journal: dict) -> dict:
    values = dict(journal["provenance"])
    if values.get("downloaded_at"):
        values["downloaded_at"] = datetime.fromisoformat(values["downloaded_at"])
    return values


def _revert_files(journal: dict, current: Path) -> dict:
    """Thread-side: set the FLAC and its sidecars aside, put the backed-up files back."""
    aside = _journal_dir(journal["track_id"]) / "reverted"
    aside.mkdir(parents=True, exist_ok=True)
    journal.update(phase="reverting", flac_identity=_file_identity(current), revert_moves=[])
    _write_journal(journal)
    for path in (current, *(current.with_suffix(s) for s in _SIDECARS)):
        if path.exists():
            journal["revert_moves"].append({"from": str(path), "to": str(aside / path.name)})
            _write_journal(journal)
            _move(path, aside / path.name)
    for moved in journal["moved"]:
        source, kept = Path(moved["from"]), Path(moved["to"])
        if moved["copied"]:
            # A shared file's sidecars were only copied; the step above may have set the live ones aside.
            if source.exists():
                continue
        elif source.exists():
            raise DownloadFailed(f"'{source.name}' already exists again in the library; nothing was reverted.")
        journal["revert_moves"].append({"from": str(kept), "to": str(source)})
        _write_journal(journal)
        _move(kept, source)
    return journal


def _undo_revert(journal: dict) -> None:
    for moved in reversed(journal.get("revert_moves", [])):
        source, target = Path(moved["from"]), Path(moved["to"])
        if target.exists() and not source.exists():
            _move(target, source)
    journal.update(phase="committed", revert_moves=[])
    journal.pop("flac_identity", None)
    _write_journal(journal)


def _backups_present(journal: dict) -> bool:
    return all(Path(m["to"]).exists() for m in journal.get("moved", []))


async def _forget_backup_window(track_id: int) -> None:
    async with async_session() as session:
        await session.execute(update(Track).where(Track.id == track_id).values(upgrade_backup_until=None)
                              .execution_options(synchronize_session=False))
        await session.commit()


async def revert_upgrade(track_id: int) -> Path:
    """Put back the file a FLAC upgrade replaced, while its backup is kept."""
    async with track_locks.hold(track_id):
        async with async_session() as session:
            track = await session.get(Track, track_id)
        journal = await asyncio.to_thread(_read_journal, track_id)
        backups = journal is not None and await asyncio.to_thread(_backups_present, journal)
        if track and track.upgrade_backup_until and not backups:
            await _forget_backup_window(track_id)
        if (not track or not backups or journal.get("phase") != "committed" or not track.upgrade_backup_until
                or track.upgrade_backup_until <= utc_now() or track.file_path != journal["new"]):
            raise DownloadFailed(NOT_REVERTIBLE)
        current, old = Path(journal["new"]), Path(journal["old"])
        try:
            journal = await asyncio.to_thread(_revert_files, journal, current)
        except Exception:
            await asyncio.to_thread(_undo_revert, journal)
            raise
        superseded = await _superseded_rows(track, _card_rel(track, "flac"), _card_rel(track, old.suffix.lstrip(".")),
                                            journal["flac_identity"], "revert")
        values = {**_restore_values(journal), "synced_at": None, "upgrade_state": "reverted",
                  "upgrade_error": None, "upgrade_backup_until": None}
        expected = {"file_path": str(current), "engine_used": "qobuz",
                    **{name: getattr(track, name) for name in _LAYOUT_FIELDS}}
        try:
            committed = await _compare_and_set(track_id, expected, values, superseded)
        except Exception:
            committed = False
            logger.exception("Committing the revert of track %d failed", track_id)
        if not committed:
            await asyncio.to_thread(_undo_revert, journal)
            raise DownloadFailed("The track changed meanwhile; nothing was reverted.")
        await asyncio.to_thread(shutil.rmtree, _journal_dir(track_id), True)
    logger.info("Track %d reverted to %s", track_id, old.name)
    return old


def _journals() -> list[dict]:
    if not UPGRADES_DIR.is_dir():
        return []
    found = []
    for folder in UPGRADES_DIR.iterdir():
        if folder.is_dir() and folder.name.isdigit() and (journal := _read_journal(int(folder.name))):
            found.append(journal)
    return found


async def recover_upgrade_journals() -> list[int]:
    """At startup: finish or undo a swap or revert a crash interrupted. Returns the track ids touched."""
    touched = []
    for journal in await asyncio.to_thread(_journals):
        phase, track_id = journal.get("phase"), journal["track_id"]
        if phase not in ("swapping", "reverting"):
            continue
        async with async_session() as session:
            track = await session.get(Track, track_id)
        touched.append(track_id)
        if phase == "reverting":
            if track and track.file_path == journal["old"] and track.upgrade_state == "reverted":
                await asyncio.to_thread(shutil.rmtree, _journal_dir(track_id), True)
            else:
                await asyncio.to_thread(_undo_revert, journal)
            continue
        if track and track.file_path == journal["new"] and track.engine_used == "qobuz":
            until = track.upgrade_backup_until or utc_now() + timedelta(days=settings.qobuz_upgrade_keep_days)
            journal.update(phase="committed", backup_until=until.isoformat())
            await asyncio.to_thread(_write_journal, journal)
            continue
        await asyncio.to_thread(_rollback_swap, journal)
        if track and track.status == "done":
            await _set_upgrade_queued(track_id)
        logger.warning("Rolled back the interrupted FLAC upgrade of track %d", track_id)
    return touched


def purge_old_backups() -> int:
    """Delete the kept files of committed upgrades whose Revert window has passed."""
    purged = 0
    now = utc_now()
    for journal in _journals():
        until = journal.get("backup_until")
        if journal.get("phase") == "committed" and until and datetime.fromisoformat(until) <= now:
            shutil.rmtree(_journal_dir(journal["track_id"]), ignore_errors=True)
            purged += 1
    return purged


# --- queueing -----------------------------------------------------------------------------

def eligibility(track: Track, *, include_unmatched: bool) -> str | None:
    return track.upgrade_eligibility(include_unmatched=include_unmatched)


async def queue_upgrades(session: AsyncSession, track_ids: list[int] | None, *, include_unmatched: bool) -> dict:
    """Mark eligible finished tracks for a FLAC upgrade and feed the worker what today's cap allows."""
    stmt = select(Track).where(Track.status == "done")
    if track_ids is not None:
        stmt = select(Track).where(Track.id.in_(track_ids))
    tracks = (await session.scalars(stmt.order_by(Track.id))).all()
    queued: list[int] = []
    skipped: dict[int, str] = {}
    for track in tracks:
        reason = eligibility(track, include_unmatched=include_unmatched)
        if reason:
            skipped[track.id] = reason
            continue
        track.upgrade_state, track.upgrade_error = "queued", None
        queued.append(track.id)
    for missing in set(track_ids or ()) - {t.id for t in tracks}:
        skipped[missing] = "not_found"
    await session.commit()
    from worker import download_worker

    await download_worker.top_up_upgrades()
    return {"queued": queued, "skipped": skipped}


async def gate_open() -> bool:
    """Whether Qobuz work could start now (used to feed queued upgrades)."""
    session = await asyncio.to_thread(qobuz_session.usable)
    if session is None or _breaker_left()[0]:
        return False
    return await asyncio.to_thread(cooldown_left) == 0 and not await asyncio.to_thread(daily_cap_reached)
