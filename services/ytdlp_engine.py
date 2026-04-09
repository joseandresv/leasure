import asyncio
import io
import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track
from services.device import sanitize_filename

logger = logging.getLogger(__name__)


async def ytdlp_download(track_id: int) -> Path | None:
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return None

        youtube_id = track.youtube_id
        fmt = "flac" if "flac" in (track.quality or "") else "mp3"
        artist = track.artist or "Unknown"
        album = track.album or "Unknown"
        title = track.title or "Unknown"
        track_num = track.track_number
        artwork_url = track.artwork_url

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
        import yt_dlp

        ydl_opts = {
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

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the output file
        for ext in [fmt, "mp3", "flac", "opus", "m4a", "webm"]:
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
            await session.commit()

    return result


async def _apply_tags(audio_path: Path, track_id: int):
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return

    def _tag():
        try:
            from mutagen import File as MutagenFile
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TCON
            from mutagen.flac import FLAC

            if audio_path.suffix == ".mp3":
                audio = MutagenFile(audio_path, easy=True)
                if audio is not None:
                    audio["title"] = track.title or ""
                    audio["artist"] = track.artist or ""
                    audio["album"] = track.album or ""
                    if track.track_number:
                        audio["tracknumber"] = str(track.track_number)
                    if track.genre:
                        audio["genre"] = track.genre
                    audio.save()
            elif audio_path.suffix == ".flac":
                audio = FLAC(audio_path)
                audio["title"] = track.title or ""
                audio["artist"] = track.artist or ""
                audio["album"] = track.album or ""
                if track.track_number:
                    audio["tracknumber"] = str(track.track_number)
                if track.genre:
                    audio["genre"] = track.genre
                audio.save()
        except Exception as e:
            logger.warning("Failed to tag %s: %s", audio_path, e)

    await asyncio.to_thread(_tag)


async def _export_sidecar_artwork(audio_path: Path, artwork_url: str | None):
    jpg_path = audio_path.with_suffix(".jpg")
    if jpg_path.exists():
        return

    if artwork_url:
        from services.artwork import download_and_save_artwork
        await download_and_save_artwork(artwork_url, jpg_path)
    else:
        # Try extracting from the audio file
        from services.spotdl_engine import _export_sidecar_artwork
        await _export_sidecar_artwork(audio_path)
