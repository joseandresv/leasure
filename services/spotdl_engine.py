import asyncio
import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track
from services.device import sanitize_filename

logger = logging.getLogger(__name__)

COOKIE_FILE = settings.data_dir / "cookies.txt"


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
        fmt = "flac" if "flac" in (track.quality or "") else "mp3"
        title = track.title or "Unknown"
        artist = track.artist or "Unknown"
        album = track.album or "Unknown"
        track_num = track.track_number
        artwork_url = track.artwork_url
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
        import yt_dlp

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

        # Try with Chrome cookies first (Premium quality), fall back without
        attempts = []
        premium_opts = {**base_opts, "cookiesfrombrowser": ("chrome",), "remote_components": ["ejs:github"]}
        attempts.append(("premium", premium_opts))
        attempts.append(("standard", {**base_opts, "remote_components": ["ejs:github"]}))

        for label, ydl_opts in attempts:
            try:
                logger.info("Trying download (%s quality)", label)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                break  # Success
            except Exception as e:
                if label == "premium":
                    logger.warning("Premium download failed, trying without cookies: %s", str(e)[:100])
                    continue
                raise  # Last attempt, let it propagate

        # Find the downloaded file
        for ext in [fmt, "mp3", "flac", "opus", "m4a", "webm"]:
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
            await session.commit()
        logger.info("Downloaded track %d to %s (engine: %s)", track_id, result,
                     "ytmusic" if video_id else "youtube")
    else:
        logger.error("No file found after download for track %d", track_id)

    return result


# Legacy functions kept for ytdlp_engine compatibility
async def _apply_tags(audio_path: Path, track_id: int):
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return

    def _tag():
        try:
            from mutagen import File as MutagenFile
            from mutagen.flac import FLAC

            if audio_path.suffix == ".mp3":
                audio = MutagenFile(audio_path, easy=True)
                if audio is not None:
                    audio["title"] = track.title or ""
                    audio["artist"] = track.artist or ""
                    audio["album"] = track.album or ""
                    if track.album_artist:
                        audio["albumartist"] = track.album_artist
                    if track.track_number:
                        audio["tracknumber"] = str(track.track_number)
                    if track.genre:
                        audio["genre"] = track.genre
                    if track.year:
                        audio["date"] = str(track.year)
                    audio.save()
            elif audio_path.suffix == ".flac":
                audio = FLAC(audio_path)
                audio["title"] = track.title or ""
                audio["artist"] = track.artist or ""
                audio["album"] = track.album or ""
                if track.album_artist:
                    audio["albumartist"] = track.album_artist
                if track.track_number:
                    audio["tracknumber"] = str(track.track_number)
                if track.genre:
                    audio["genre"] = track.genre
                if track.year:
                    audio["date"] = str(track.year)
                audio.save()
            logger.info("Tagged %s", audio_path)
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
        def _extract():
            try:
                from mutagen import File as MutagenFile
                from PIL import Image
                import io

                audio = MutagenFile(audio_path)
                if audio is None:
                    return

                art_data = None
                if hasattr(audio, "tags") and audio.tags:
                    for key in audio.tags:
                        if str(key).startswith("APIC"):
                            art_data = audio.tags[key].data
                            break
                if art_data is None and hasattr(audio, "pictures"):
                    for pic in audio.pictures:
                        art_data = pic.data
                        break

                if art_data:
                    img = Image.open(io.BytesIO(art_data))
                    img = img.resize((settings.artwork_size, settings.artwork_size), Image.LANCZOS)
                    img.convert("RGB").save(jpg_path, "JPEG", quality=90)
                    logger.info("Exported album art to %s", jpg_path)
            except Exception as e:
                logger.warning("Failed to export album art for %s: %s", audio_path, e)

        await asyncio.to_thread(_extract)
