"""The one YouTube download pipeline: match a source, stage the download, read the
provenance yt-dlp reports, verify it, then move the result into the library."""

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from config import settings
from db import async_session
from models import Track
from services.device import sanitize_filename
from services.ytdlp_opts import CookieSessionRefused, run_with_attempts

logger = logging.getLogger(__name__)

# Prefer the Premium AAC 256 stream, then Premium Opus, then any AAC (the H2 plays
# M4A natively and 141 is 44.1 kHz with no Opus lowpass).
SOURCE_SELECTOR = "bestaudio[format_id=141]/bestaudio[format_id=774]/bestaudio[ext=m4a]/bestaudio"
MIN_MATCH_SCORE = 4.0
DAILY_COUNTER_FILE = settings.data_dir / "yt_daily.json"
MAX_DOWNLOADS_PER_DAY = settings.yt_max_downloads_per_day

_PREMIUM_ITAGS = {"141", "774"}
_BITRATE_TIERS = (48, 64, 96, 128, 160, 192, 256, 264, 320)
_VARIANT_WORDS = ("live", "remix", "cover", "karaoke", "nightcore", "sped up", "slowed", "8d", "instrumental")
_TITLE_NOISE = ("official music video", "official video", "official audio", "official visualizer",
                "lyric video", "lyrics", "audio only", "hd", "hq", "4k")
_AUDIO_SUFFIXES = (".m4a", ".mp3", ".opus", ".ogg", ".webm", ".flac", ".wav")
_NETWORK_MARKERS = ("urlopen error", "temporary failure in name resolution", "timed out", "timeout",
                    "connection reset", "network is unreachable", "failed to resolve", "getaddrinfo")
_GONE_MARKERS = ("video unavailable", "private video", "removed by the uploader", "has been terminated")


class DownloadFailed(RuntimeError):
    """The track cannot be downloaded; the message is shown to the user as-is."""


def _download_failure(error: Exception) -> DownloadFailed:
    """A yt-dlp exception as a sentence the queue can show and the user can act on."""
    message = str(error)
    lowered = message.lower()
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return DownloadFailed("Could not reach YouTube — check the network connection, then retry.")
    if any(marker in lowered for marker in _GONE_MARKERS):
        return DownloadFailed("YouTube no longer serves this recording (unavailable, private or removed).")
    if "ffmpeg" in lowered:
        return DownloadFailed(f"ffmpeg could not convert the download: {message[:200]}")
    return DownloadFailed(f"yt-dlp could not download the track: {message[:200]}")


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    artist: str = ""
    duration_s: int | None = None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    quality: str
    container: str
    format_id: str | None
    codec: str | None
    bitrate_kbps: int | None
    sample_rate: int | None
    premium_used: bool
    transcoded: bool
    verification: str
    filesize: int | None
    source_title: str | None


def _normalise(text: str) -> str:
    lowered = text.lower()
    for noise in _TITLE_NOISE:
        lowered = lowered.replace(noise, " ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())


def _variant_penalty(candidate_title: str, wanted_title: str) -> float:
    """3 points per variant word the candidate adds and the request did not ask for."""
    candidate = candidate_title.lower()
    wanted = wanted_title.lower()
    hits = [w for w in _VARIANT_WORDS if w not in wanted and re.search(rf"\b{re.escape(w)}\b", candidate)]
    return 3.0 * len(hits)


def score_candidate(candidate: Candidate, title: str, artist: str, duration_ms: int = 0) -> float | None:
    """Match score out of 6, or None when the candidate is disqualified."""
    wanted_s = (duration_ms or 0) / 1000
    off_by = abs(candidate.duration_s - wanted_s) if wanted_s and candidate.duration_s else None
    if off_by is not None and off_by > 30:
        return None

    score = 3.0 * SequenceMatcher(None, _normalise(title), _normalise(candidate.title)).ratio()
    primary_artist = _normalise(artist.split(",")[0])
    candidate_artist = _normalise(candidate.artist)
    if primary_artist and candidate_artist:
        if primary_artist in candidate_artist:
            score += 2.0
        else:
            score += 2.0 * SequenceMatcher(None, primary_artist, candidate_artist).ratio()
    if off_by is not None and off_by <= 10:
        score += 1.0
    return score - _variant_penalty(candidate.title, title)


def pick_best(candidates: list[Candidate], title: str, artist: str, duration_ms: int = 0) -> tuple[Candidate, float] | None:
    """Highest-scoring candidate, or None when none of them clears MIN_MATCH_SCORE."""
    scored = [(c, score) for c in candidates if (score := score_candidate(c, title, artist, duration_ms)) is not None]
    if not scored:
        return None
    best, score = max(scored, key=lambda pair: pair[1])
    if score < MIN_MATCH_SCORE:
        logger.info("Best candidate for '%s - %s' scored %.1f, below %.1f", artist, title, score, MIN_MATCH_SCORE)
        return None
    return best, score


def search_ytmusic(title: str, artist: str, duration_ms: int = 0) -> tuple[Candidate, float] | None:
    """Best-scoring YouTube Music song for a Spotify-sourced track."""
    try:
        from ytmusicapi import YTMusic

        yt = YTMusic()
        query = f"{artist} {title}"
        results = yt.search(query, filter="songs", limit=5) or yt.search(query, limit=5)
    except Exception as e:
        logger.warning("YouTube Music search failed for '%s - %s': %s", artist, title, e)
        return None

    candidates = [
        Candidate(
            video_id=r["videoId"],
            title=r.get("title") or "",
            artist=", ".join(a["name"] for a in r.get("artists") or [] if a.get("name")),
            duration_s=r.get("duration_seconds"),
        )
        for r in results or []
        if r.get("videoId")
    ]
    return pick_best(candidates, title, artist, duration_ms)


def search_youtube(title: str, artist: str, duration_ms: int = 0) -> tuple[Candidate, float] | None:
    """Score five plain-YouTube search results; never trust ytsearch1 blindly."""
    info = run_with_attempts(f"ytsearch5:{artist} - {title}", {"quiet": True, "extract_flat": "in_playlist"},
                             download=False)
    candidates = [
        Candidate(
            video_id=e["id"],
            title=e.get("title") or "",
            artist=e.get("channel") or e.get("uploader") or "",
            duration_s=e.get("duration"),
        )
        for e in info.get("entries") or []
        if e.get("id")
    ]
    return pick_best(candidates, title, artist, duration_ms)


def downloads_today() -> int:
    try:
        state = json.loads(DAILY_COUNTER_FILE.read_text())
    except (OSError, ValueError):
        return 0
    return int(state.get("count", 0)) if state.get("date") == date.today().isoformat() else 0


def daily_cap_reached() -> bool:
    return downloads_today() >= MAX_DOWNLOADS_PER_DAY


def record_daily_download() -> int:
    count = downloads_today() + 1
    DAILY_COUNTER_FILE.write_text(json.dumps({"date": date.today().isoformat(), "count": count}))
    return count


def _library_stem(artist: str, album: str, title: str, track_number: int | None) -> Path:
    prefix = f"{track_number:02d} - " if track_number else ""
    return (settings.library_dir / sanitize_filename(artist) / sanitize_filename(album)
            / sanitize_filename(f"{prefix}{title}"))


def _build_opts(staging_dir: Path, to_mp3: bool) -> dict:
    postprocessor = (
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(settings.mp3_bitrate)}
        if to_mp3
        else {"key": "FFmpegExtractAudio", "preferredcodec": "best"}
    )
    return {
        "format": SOURCE_SELECTOR,
        "outtmpl": str(staging_dir / "source.%(ext)s"),
        "postprocessors": [postprocessor],
        "quiet": True,
    }


def _chosen_download(info: dict) -> dict:
    downloads = info.get("requested_downloads") or []
    return downloads[0] if downloads else {}


def _field(chosen: dict, info: dict, key: str):
    value = chosen.get(key)
    return info.get(key) if value is None else value


def _staged_file(info: dict, staging_dir: Path) -> Path | None:
    for reported in (_chosen_download(info).get("filepath"), info.get("filepath")):
        if reported and Path(reported).exists():
            return Path(reported)
    # yt-dlp versions disagree about which dict keeps the post-processed path. The
    # staging dir holds this download and nothing else, so a lone audio file there is it.
    files = [p for p in staging_dir.iterdir() if p.suffix.lower() in _AUDIO_SUFFIXES]
    return files[0] if len(files) == 1 else None


def _bitrate_tier(abr: float) -> int:
    return min(_BITRATE_TIERS, key=lambda tier: abs(tier - abr))


def _quality_label(codec: str | None, bitrate_kbps: int | None) -> str:
    lowered = (codec or "").lower()
    if lowered.startswith(("mp4a", "aac")):
        family = "aac"
    elif "opus" in lowered:
        family = "opus"
    elif "vorbis" in lowered:
        family = "vorbis"
    elif "mp3" in lowered:
        family = "mp3"
    else:
        family = lowered.split(".")[0] or "unknown"
    return f"{family}_{bitrate_kbps}" if bitrate_kbps else family


def _verify_duration(reported_s: float | None, duration_ms: int) -> str:
    if not duration_ms or not reported_s:
        return "unverified"
    return "ok" if abs(reported_s - duration_ms / 1000) <= 10 else "duration_mismatch"


def _move_into_library(staged: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(staged, dest)
    except OSError:
        # download_dir and library_dir may sit on different filesystems (ext4 vs drvfs).
        shutil.move(str(staged), str(dest))
        with open(dest, "r+b") as handle:
            os.fsync(handle.fileno())


def _download_to_library(url: str, staging_dir: Path, stem: Path, to_mp3: bool, duration_ms: int) -> DownloadResult:
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            info = run_with_attempts(url, _build_opts(staging_dir, to_mp3))
        except (DownloadFailed, CookieSessionRefused):
            raise
        except Exception as e:
            raise _download_failure(e) from e
        staged = _staged_file(info, staging_dir)
        if not staged:
            raise DownloadFailed("yt-dlp downloaded nothing playable — check that ffmpeg is installed and on PATH.")

        chosen = _chosen_download(info)
        format_id = _field(chosen, info, "format_id")
        codec = _field(chosen, info, "acodec")
        abr = _field(chosen, info, "abr")
        note = _field(chosen, info, "format_note") or ""
        bitrate = int(_bitrate_tier(abr)) if abr else None
        dest = stem.with_suffix(staged.suffix)
        _move_into_library(staged, dest)
        return DownloadResult(
            path=dest,
            quality=f"mp3_{settings.mp3_bitrate}" if to_mp3 else _quality_label(codec, bitrate),
            container=dest.suffix.lstrip("."),
            format_id=str(format_id) if format_id is not None else None,
            codec=codec,
            bitrate_kbps=bitrate,
            sample_rate=_field(chosen, info, "asr"),
            premium_used=str(format_id) in _PREMIUM_ITAGS or "premium" in note.lower(),
            transcoded=to_mp3,
            verification=_verify_duration(info.get("duration"), duration_ms),
            filesize=_field(chosen, info, "filesize") or _field(chosen, info, "filesize_approx"),
            source_title=info.get("title"),
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


async def _resolve_source(title: str, artist: str, duration_ms: int) -> tuple[str, str, tuple[Candidate, float]]:
    match = await asyncio.to_thread(search_ytmusic, title, artist, duration_ms)
    engine = "ytmusic"
    if not match:
        match = await asyncio.to_thread(search_youtube, title, artist, duration_ms)
        engine = "youtube-search"
    if not match:
        raise DownloadFailed(f"No confident match on YouTube for '{artist} - {title}' — "
                             "try another version, or download it from YouTube Music search.")
    logger.info("Matched '%s - %s' to %s ('%s', score %.1f)", artist, title, match[0].video_id, match[0].title, match[1])
    return match[0].video_id, engine, match


async def _record_provenance(track_id: int, result: DownloadResult, engine: str,
                             match: tuple[Candidate, float] | None) -> None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return
        track.file_size = result.filesize or result.path.stat().st_size
        track.engine_used = engine
        track.format = result.container
        track.quality = result.quality
        track.source_format_id = result.format_id
        track.source_codec = result.codec
        track.source_bitrate_kbps = result.bitrate_kbps
        track.source_sample_rate = result.sample_rate
        track.premium_used = result.premium_used
        track.transcoded = result.transcoded
        track.is_lossless = False
        track.verification = result.verification
        track.matched_title = match[0].title if match else result.source_title
        track.match_score = match[1] if match else None
        await session.commit()


async def download(track_id: int) -> Path:
    """Download a track from YouTube Music, tag it, and record what was actually fetched."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            raise DownloadFailed("This track is no longer in the library.")
        youtube_id = track.youtube_id
        quality = track.quality or "mp3_320"
        title = track.title or "Unknown"
        artist = track.artist or "Unknown"
        album = track.album or "Unknown"
        track_number = track.track_number
        duration_ms = track.duration_ms or 0

    match: tuple[Candidate, float] | None = None
    if youtube_id:
        video_id, engine = youtube_id, "youtube"
    else:
        video_id, engine, match = await _resolve_source(title, artist, duration_ms)

    # music.youtube.com, not www: the Premium 141/774 streams are only offered there.
    result = await asyncio.to_thread(
        _download_to_library,
        f"https://music.youtube.com/watch?v={video_id}",
        settings.download_dir / str(track_id),
        _library_stem(artist, album, title, track_number),
        quality != "native",
        duration_ms,
    )

    from services.tagger import apply_full_metadata

    try:
        await apply_full_metadata(result.path, track_id)
    except Exception as e:
        logger.exception("Tagging failed for track %d", track_id)
        raise DownloadFailed(f"Downloaded, but tagging failed: {str(e)[:200]}") from e
    await _record_provenance(track_id, result, engine, match)
    await asyncio.to_thread(record_daily_download)
    logger.info("Downloaded track %d to %s (%s, format %s, verification %s)",
                track_id, result.path, result.quality, result.format_id, result.verification)
    if result.verification == "duration_mismatch":
        logger.warning("Track %d duration differs from the requested recording by more than 10 s", track_id)
    return result.path
