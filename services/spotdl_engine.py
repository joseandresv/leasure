import asyncio
import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track
from services.device import sanitize_filename
from services.ytdlp_opts import download_with_fallback

logger = logging.getLogger(__name__)


def _search_ytmusic(title: str, artist: str, duration_ms: int = 0) -> str | None:
    """Search YouTube Music for a track, return video ID or None."""
    try:
        from ytmusicapi import YTMusic

        yt = YTMusic()
        query = f"{artist} {title}"
        results = yt.search(query, filter="songs", limit=5)

        if not results:
            # Try without filter
            results = yt.search(query, limit=5)

        if not results:
            return None

        # Score results by title/artist similarity
        best = None
        best_score = -1

        for r in results:
            if not r.get("videoId"):
                continue

            r_title = r.get("title", "").lower()
            r_artist = ", ".join(a["name"] for a in r.get("artists", []) if a.get("name")).lower()

            score = 0
            if title.lower() in r_title or r_title in title.lower():
                score += 2
            if artist.lower().split(",")[0].strip() in r_artist:
                score += 2

            # Duration match (within 10 seconds)
            if duration_ms and r.get("duration_seconds"):
                diff = abs((duration_ms / 1000) - r["duration_seconds"])
                if diff < 10:
                    score += 1

            if score > best_score:
                best_score = score
                best = r["videoId"]

        return best
    except Exception as e:
        logger.warning("YouTube Music search failed: %s", e)
        return None


async def spotdl_download(track_id: int) -> Path | None:
    """Download a Spotify track via YouTube Music (better quality) with yt-dlp."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

        spotify_uri = track.spotify_uri
        quality = track.quality or "mp3_320"
        native = quality == "native"  # keep original container, no re-encode
        fmt = "flac" if "flac" in quality else "mp3"
        title = track.title or "Unknown"
        artist = track.artist or "Unknown"
        album = track.album or "Unknown"
        track_num = track.track_number
        duration_ms = track.duration_ms or 0

    if not spotify_uri:
        logger.error("Track %d has no Spotify URI", track_id)
        return None

    # Step 1: Search YouTube Music for the best match
    video_id = await asyncio.to_thread(_search_ytmusic, title, artist, duration_ms)

    if video_id:
        url = f"https://music.youtube.com/watch?v={video_id}"
        logger.info("Found YouTube Music match for '%s - %s': %s", artist, title, url)
    else:
        # Fallback to regular YouTube search
        url = f"ytsearch1:{artist} - {title}"
        logger.info("No YouTube Music match, falling back to YouTube search: %s", artist + " - " + title)

    # Step 2: Build output path
    artist_dir = sanitize_filename(artist)
    album_dir = sanitize_filename(album)
    num_prefix = f"{track_num:02d} - " if track_num else ""
    filename = sanitize_filename(f"{num_prefix}{title}")
    output_dir = settings.library_dir / artist_dir / album_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / f"{filename}.%(ext)s")

    # Step 3: Download with yt-dlp
    def _do_download() -> Path | None:
        if native:
            # Prefer the best AAC/M4A stream and keep it verbatim — no ffmpeg re-encode.
            # The H2 plays M4A/AAC natively; this is the highest fidelity our sources can give.
            base_opts = {
                # Prefer the AAC stream (the H2 plays M4A natively). If only Opus/WebM
                # exists, "best" remuxes it into a taggable .opus without re-encoding.
                "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio",
                "outtmpl": output_template,
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}],
                "quiet": True,
                "no_warnings": True,
            }
        else:
            base_opts = {
                "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": output_template,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": fmt,
                        "preferredquality": str(settings.mp3_bitrate) if fmt == "mp3" else "0",
                    }
                ],
                "quiet": True,
                "no_warnings": True,
            }

        if url.startswith("ytsearch"):
            base_opts["default_search"] = "ytsearch1"

        download_with_fallback(url, base_opts)

        # Find the downloaded file
        search_exts = ["m4a", "webm", "opus", "mp3", "flac"] if native else [fmt, "mp3", "flac", "opus", "m4a", "webm"]
        for ext in search_exts:
            candidate = output_dir / f"{filename}.{ext}"
            if candidate.exists():
                return candidate

        # Fallback: most recent file
        files = sorted(
            [f for f in output_dir.iterdir() if f.suffix in (".mp3", ".flac", ".opus", ".m4a", ".wav")],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None

    result = await asyncio.to_thread(_do_download)

    if result:
        # Apply full H2-compatible metadata (tags, embedded art, sidecar jpg, lyrics)
        from services.tagger import apply_full_metadata
        await apply_full_metadata(result, track_id)

        async with async_session() as session:
            db_track = await session.get(Track, track_id)
            db_track.file_size = result.stat().st_size
            db_track.engine_used = "ytmusic" if video_id else "youtube"
            if native:
                # Record the actual container we kept (m4a / webm / opus)
                db_track.format = result.suffix.lstrip(".")
            await session.commit()
        logger.info("Downloaded track %d to %s (engine: %s)", track_id, result,
                     "ytmusic" if video_id else "youtube")
    else:
        logger.error("No file found after download for track %d", track_id)

    return result
