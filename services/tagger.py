"""
Full metadata tagger for HIFI WALKER H2 compatibility.

The H2 reads:
- ID3v2 tags: TIT2 (title), TPE1 (artist), TALB (album), TPE2 (album artist),
  TRCK (track number), TPOS (disc number), TCON (genre), TDRC (year), APIC (cover art)
- FLAC Vorbis comments: title, artist, album, albumartist, tracknumber, discnumber, genre, date
- Sidecar .jpg file with same name as audio (for album art display)
- Sidecar .lrc file with same name as audio (for lyrics display)
"""
import asyncio
import io
import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track

# Cache genre lookups to avoid hitting MusicBrainz repeatedly
_genre_cache: dict[str, str | None] = {}

logger = logging.getLogger(__name__)


async def apply_full_metadata(audio_path: Path, track_id: int):
    """Apply complete H2-compatible metadata to a downloaded track."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return

    # 0. Fetch genre if missing (from Spotify artist or YouTube Music search)
    if not track.genre:
        genre = await _fetch_genre(track)
        if genre:
            track.genre = genre
            await session.commit()

    # 1. Apply ID3/Vorbis tags
    await _apply_tags(audio_path, track)

    # 2. Embed album art into the audio file
    if track.artwork_url:
        await _embed_artwork(audio_path, track.artwork_url)

    # 3. Export sidecar .jpg for H2 album art display
    await _export_sidecar_jpg(audio_path, track.artwork_url)

    # 4. Fetch and save .lrc lyrics
    from services.lyrics import save_lrc
    await save_lrc(
        audio_path,
        title=track.title or "",
        artist=track.artist or "",
        album=track.album or "",
        duration_ms=track.duration_ms or 0,
    )

    logger.info("Full H2 metadata applied to %s", audio_path.name)


async def _fetch_genre(track) -> str | None:
    """Fetch genre from MusicBrainz (free, reliable genre database)."""
    artist_name = (track.artist or "").split(",")[0].strip()
    if not artist_name:
        return None

    if artist_name in _genre_cache:
        cached = _genre_cache[artist_name]
        if cached:
            logger.debug("Genre cache hit for '%s': %s", artist_name, cached)
        return cached

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://musicbrainz.org/ws/2/artist/",
                params={"query": artist_name, "limit": 1, "fmt": "json"},
                headers={"User-Agent": "Leasure/0.1 (music-downloader)"},
                timeout=10,
            )
            if r.status_code != 200:
                return None

            data = r.json()
            artists = data.get("artists", [])
            if not artists:
                return None

            tags = artists[0].get("tags", [])
            if tags:
                sorted_tags = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
                genre = ", ".join(t["name"] for t in sorted_tags[:3])
                logger.info("Found genre for '%s': %s (via MusicBrainz)", artist_name, genre)
                _genre_cache[artist_name] = genre
                return genre
    except Exception as e:
        logger.debug("MusicBrainz genre lookup failed for '%s': %s", artist_name, e)

    _genre_cache[artist_name] = None
    return None


async def _apply_tags(audio_path: Path, track: Track):
    """Write ID3v2.4 (MP3) or Vorbis comments (FLAC) tags."""
    def _do_tag():
        try:
            if audio_path.suffix == ".mp3":
                _tag_mp3(audio_path, track)
            elif audio_path.suffix == ".flac":
                _tag_flac(audio_path, track)
            else:
                logger.debug("Unknown format %s, skipping tags", audio_path.suffix)
        except Exception as e:
            logger.warning("Failed to tag %s: %s", audio_path, e)

    await asyncio.to_thread(_do_tag)


def _tag_mp3(audio_path: Path, track: Track):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TCON, TDRC

    audio = MP3(audio_path)
    if audio.tags is None:
        audio.add_tags()

    tags = audio.tags
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.delall("TPE2")
    tags.delall("TRCK")
    tags.delall("TPOS")
    tags.delall("TCON")
    tags.delall("TDRC")

    if track.title:
        tags.add(TIT2(encoding=3, text=track.title))
    if track.artist:
        tags.add(TPE1(encoding=3, text=track.artist))
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))
    if track.album_artist:
        tags.add(TPE2(encoding=3, text=track.album_artist))
    elif track.artist:
        # H2 uses album artist for browsing; default to track artist
        tags.add(TPE2(encoding=3, text=track.artist.split(",")[0].strip()))
    if track.track_number:
        tags.add(TRCK(encoding=3, text=str(track.track_number)))
    if track.disc_number:
        tags.add(TPOS(encoding=3, text=str(track.disc_number)))
    if track.genre:
        tags.add(TCON(encoding=3, text=track.genre))
    if track.year:
        tags.add(TDRC(encoding=3, text=str(track.year)))

    audio.save()
    logger.debug("MP3 tags applied to %s", audio_path.name)


def _tag_flac(audio_path: Path, track: Track):
    from mutagen.flac import FLAC

    audio = FLAC(audio_path)

    if track.title:
        audio["title"] = track.title
    if track.artist:
        audio["artist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.album_artist:
        audio["albumartist"] = track.album_artist
    elif track.artist:
        audio["albumartist"] = track.artist.split(",")[0].strip()
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
    if track.disc_number:
        audio["discnumber"] = str(track.disc_number)
    if track.genre:
        audio["genre"] = track.genre
    if track.year:
        audio["date"] = str(track.year)

    audio.save()
    logger.debug("FLAC tags applied to %s", audio_path.name)


async def _embed_artwork(audio_path: Path, artwork_url: str):
    """Download artwork and embed it into the audio file."""
    def _do_embed(art_data: bytes):
        try:
            from PIL import Image

            # Resize to 500x500 for embedded art (keeps file size reasonable)
            img = Image.open(io.BytesIO(art_data))
            img = img.resize((settings.artwork_size, settings.artwork_size), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=90)
            jpeg_data = buf.getvalue()

            if audio_path.suffix == ".mp3":
                _embed_mp3(audio_path, jpeg_data)
            elif audio_path.suffix == ".flac":
                _embed_flac(audio_path, jpeg_data)

            logger.debug("Embedded artwork in %s", audio_path.name)
        except Exception as e:
            logger.warning("Failed to embed artwork in %s: %s", audio_path, e)

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(artwork_url, follow_redirects=True, timeout=15)
            resp.raise_for_status()
            await asyncio.to_thread(_do_embed, resp.content)
    except Exception as e:
        logger.warning("Failed to download artwork from %s: %s", artwork_url, e)


def _embed_mp3(audio_path: Path, jpeg_data: bytes):
    from mutagen.mp3 import MP3
    from mutagen.id3 import APIC

    audio = MP3(audio_path)
    if audio.tags is None:
        audio.add_tags()

    # Remove existing art
    audio.tags.delall("APIC")

    audio.tags.add(APIC(
        encoding=3,  # UTF-8
        mime="image/jpeg",
        type=3,  # Front cover
        desc="Cover",
        data=jpeg_data,
    ))
    audio.save()


def _embed_flac(audio_path: Path, jpeg_data: bytes):
    from mutagen.flac import FLAC, Picture

    audio = FLAC(audio_path)
    audio.clear_pictures()

    pic = Picture()
    pic.type = 3  # Front cover
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = jpeg_data
    audio.add_picture(pic)
    audio.save()


async def _export_sidecar_jpg(audio_path: Path, artwork_url: str | None):
    """Save album art as a .jpg sidecar file (H2 reads this for display)."""
    jpg_path = audio_path.with_suffix(".jpg")
    if jpg_path.exists():
        return

    if artwork_url:
        from services.artwork import download_and_save_artwork
        await download_and_save_artwork(artwork_url, jpg_path)
    else:
        # Try extracting from embedded art in the audio file
        def _extract():
            try:
                from mutagen import File as MutagenFile
                from PIL import Image

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
                    logger.info("Extracted album art to %s", jpg_path)
            except Exception as e:
                logger.warning("Failed to extract album art for %s: %s", audio_path, e)

        await asyncio.to_thread(_extract)
