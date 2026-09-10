import asyncio
import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track
from services.device import sanitize_filename
from services.ytdlp_opts import download_with_fallback

logger = logging.getLogger(__name__)


async def ytdlp_download(track_id: int) -> Path | None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

        youtube_id = track.youtube_id
        quality = track.quality or "mp3_320"
        native = quality == "native"  # keep original container, no re-encode
        fmt = "flac" if "flac" in quality else "mp3"
        artist = track.artist or "Unknown"
        album = track.album or "Unknown"
        title = track.title or "Unknown"
        track_num = track.track_number

    if not youtube_id:
        logger.error("Track %d has no YouTube ID", track_id)
        return None

    # Build output path
    artist_dir = sanitize_filename(artist)
    album_dir = sanitize_filename(album)
    num_prefix = f"{track_num:02d} - " if track_num else ""
    filename = sanitize_filename(f"{num_prefix}{title}")
    output_dir = settings.library_dir / artist_dir / album_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / f"{filename}.%(ext)s")

    url = f"https://www.youtube.com/watch?v={youtube_id}"

    def _do_download() -> Path | None:
        if native:
            # Keep the best AAC/M4A stream verbatim — no ffmpeg re-encode.
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

        download_with_fallback(url, base_opts)

        # Find the output file
        search_exts = ["m4a", "webm", "opus", "mp3", "flac"] if native else [fmt, "mp3", "flac", "opus", "m4a", "webm"]
        for ext in search_exts:
            candidate = output_dir / f"{filename}.{ext}"
            if candidate.exists():
                return candidate

        # Fallback: most recent file in output dir
        files = sorted(output_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            if f.suffix in (".mp3", ".flac", ".opus", ".m4a", ".wav"):
                return f
        return None

    result = await asyncio.to_thread(_do_download)

    if result:
        # Apply full H2-compatible metadata (tags, embedded art, sidecar jpg, lyrics)
        from services.tagger import apply_full_metadata
        await apply_full_metadata(result, track_id)

        # Update DB
        async with async_session() as session:
            db_track = await session.get(Track, track_id)
            db_track.file_size = result.stat().st_size
            db_track.engine_used = "yt-dlp"
            if native:
                # Record the actual container we kept (m4a / webm / opus)
                db_track.format = result.suffix.lstrip(".")
            await session.commit()

    return result
