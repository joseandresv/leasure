"""
Full metadata tagger for HIFI WALKER H2 compatibility.

The H2 reads:
- ID3v2 tags: TIT2 (title), TPE1 (artist), TALB (album), TPE2 (album artist),
  TRCK (track number, "n/total"), TPOS (disc number, "d/total"), TCON (genre),
  TDRC/TYER (year), APIC (cover art), written as ID3v2.3 by default (settings.id3_version)
- FLAC/Opus Vorbis comments: title, artist, album, albumartist, tracknumber, tracktotal,
  discnumber, disctotal, genre, date
- M4A iTunes atoms, with trkn/disk as (number, total) pairs
- Sidecar .jpg file with same name as audio (for album art display)
- Sidecar .lrc file with same name as audio (for lyrics display)

Each file is opened and saved once: text tags and the cover picture go in together.
"""
import asyncio
import logging
from pathlib import Path

from config import settings
from db import async_session
from models import Track
from services import artwork

logger = logging.getLogger(__name__)


async def apply_full_metadata(audio_path: Path, track_id: int):
    """Apply complete H2-compatible metadata to a downloaded track."""
    async with async_session() as session:
        track = await session.get(Track, track_id)
        if not track:
            return

        # 0. Fetch genre if missing (MusicBrainz) and persist it while the
        #    session is still open so the library view / genre map see it.
        if not track.genre:
            genre = await _fetch_genre(track)
            if genre:
                track.genre = genre
                await session.commit()

    # 1. Fetch the cover once; it is both embedded and written as the sidecar
    cover_jpeg = await _prepare_cover(track.artwork_url)

    # 2. Tags and cover art in a single open/save of the audio file
    await asyncio.to_thread(_write_tags, audio_path, track, cover_jpeg)

    # 3. Export sidecar .jpg for H2 album art display
    await _export_sidecar_jpg(audio_path, cover_jpeg)

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
    """Comma-joined MusicBrainz genres for Track.genre (only the first reaches the file)."""
    from services import musicbrainz

    genres = await musicbrainz.genres_for_track(
        isrc=track.isrc,
        title=track.title or "",
        artist=track.artist or "",
        album=track.album,
    )
    return ", ".join(genres) or None


def primary_genre(genre: str | None) -> str | None:
    """The H2 groups its Genre category by the exact tag text, so a comma-joined
    list like "rock, alternative rock, indie" becomes its own bucket. Write only
    the first (highest-ranked) genre to the file; the full list stays in the DB
    for the genre map."""
    if not genre:
        return None
    first = genre.split(",")[0].strip()
    return first or None


def primary_artist(artist: str | None) -> str | None:
    """First artist of a comma-joined list (used as the album-artist default)."""
    if not artist:
        return None
    first = artist.split(",")[0].strip()
    return first or None


TAGGABLE_SUFFIXES = (".mp3", ".flac", ".m4a", ".mp4", ".opus", ".ogg")


async def _prepare_cover(artwork_url: str | None) -> bytes | None:
    if not artwork_url:
        return None
    data = await artwork.fetch_artwork(artwork_url)
    if data is None:
        return None
    return await asyncio.to_thread(artwork.prepare_cover, data, settings.artwork_size)


def _write_tags(audio_path: Path, track: Track, cover_jpeg: bytes | None = None):
    """Write ID3v2 (MP3), Vorbis comments (FLAC/Opus/Ogg) or iTunes atoms (M4A) tags
    together with the cover picture, in one open/save of the file."""
    try:
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            _tag_mp3(audio_path, track, cover_jpeg)
        elif suffix == ".flac":
            _tag_flac(audio_path, track, cover_jpeg)
        elif suffix in (".m4a", ".mp4"):
            _tag_mp4(audio_path, track, cover_jpeg)
        elif suffix in (".opus", ".ogg"):
            _tag_vorbis(audio_path, track, cover_jpeg)
        else:
            logger.warning("Unknown format %s, skipping tags", audio_path.suffix)
    except Exception as e:
        logger.warning("Failed to tag %s: %s", audio_path, e)


def _numbering(number: int, total: int | None) -> str:
    """ID3 "n/total" form; the bare number when the total is unknown."""
    return f"{number}/{total}" if total else str(number)


def _tag_mp3(audio_path: Path, track: Track, cover_jpeg: bytes | None = None):
    from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
    from mutagen.mp3 import MP3

    audio = MP3(audio_path)
    if audio.tags is None:
        audio.add_tags()

    tags = audio.tags
    for frame in ("TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS", "TCON", "TDRC"):
        tags.delall(frame)

    # ID3v2.3 has no UTF-8 text encoding; UTF-16 is the only Unicode one it defines.
    encoding = 1 if settings.id3_version == 3 else 3

    if track.title:
        tags.add(TIT2(encoding=encoding, text=track.title))
    if track.artist:
        tags.add(TPE1(encoding=encoding, text=track.artist))
    if track.album:
        tags.add(TALB(encoding=encoding, text=track.album))
    if track.album_artist:
        tags.add(TPE2(encoding=encoding, text=track.album_artist))
    elif primary_artist(track.artist):
        # H2 uses album artist for browsing; default to the primary track artist
        tags.add(TPE2(encoding=encoding, text=primary_artist(track.artist)))
    if track.track_number:
        tags.add(TRCK(encoding=encoding, text=_numbering(track.track_number, getattr(track, "total_tracks", None))))
    if track.disc_number:
        tags.add(TPOS(encoding=encoding, text=_numbering(track.disc_number, getattr(track, "total_discs", None))))
    if primary_genre(track.genre):
        tags.add(TCON(encoding=encoding, text=primary_genre(track.genre)))
    if track.year:
        tags.add(TDRC(encoding=encoding, text=str(track.year)))
    # APIC is replaced, never cleared: a re-tag with no fetched cover (offline, dead
    # artwork URL) must keep the art already embedded -- the sidecar export reads it back.
    if cover_jpeg:
        tags.delall("APIC")
        tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="Cover", data=cover_jpeg))

    # mutagen does not downconvert on save: v2.3 needs this to turn TDRC into TYER.
    if settings.id3_version == 3:
        tags.update_to_v23()

    # v1=0: the H2 reads ID3v2 only, and a v1 trailer just adds a truncated duplicate.
    audio.save(v1=0, v2_version=settings.id3_version)
    logger.debug("MP3 tags applied to %s", audio_path.name)


def _write_vorbis_comments(audio, track: Track):
    if track.title:
        audio["title"] = track.title
    if track.artist:
        audio["artist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.album_artist:
        audio["albumartist"] = track.album_artist
    elif primary_artist(track.artist):
        audio["albumartist"] = primary_artist(track.artist)
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
        total_tracks = getattr(track, "total_tracks", None)
        if total_tracks:
            audio["tracktotal"] = str(total_tracks)
    if track.disc_number:
        audio["discnumber"] = str(track.disc_number)
        total_discs = getattr(track, "total_discs", None)
        if total_discs:
            audio["disctotal"] = str(total_discs)
    if primary_genre(track.genre):
        audio["genre"] = primary_genre(track.genre)
    if track.year:
        audio["date"] = str(track.year)


def _tag_flac(audio_path: Path, track: Track, cover_jpeg: bytes | None = None):
    from mutagen.flac import FLAC

    audio = FLAC(audio_path)
    _write_vorbis_comments(audio, track)
    if cover_jpeg:
        audio.clear_pictures()
        audio.add_picture(_flac_picture(cover_jpeg))
    audio.save()
    logger.debug("FLAC tags applied to %s", audio_path.name)


def _tag_vorbis(audio_path: Path, track: Track, cover_jpeg: bytes | None = None):
    """Opus / Ogg Vorbis: same comment keys as FLAC, picture as a base64 comment."""
    import base64

    from mutagen import File as MutagenFile

    audio = MutagenFile(audio_path)
    if audio is None:
        raise ValueError(f"mutagen cannot open {audio_path.name}")
    if audio.tags is None:
        audio.add_tags()
    _write_vorbis_comments(audio, track)
    if cover_jpeg:
        block = _flac_picture(cover_jpeg).write()
        audio["metadata_block_picture"] = [base64.b64encode(block).decode("ascii")]
    audio.save()
    logger.debug("Vorbis tags applied to %s", audio_path.name)


def _flac_picture(jpeg_data: bytes):
    from mutagen.flac import Picture

    pic = Picture()
    pic.type = 3  # Front cover
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = jpeg_data
    return pic


def _tag_mp4(audio_path: Path, track: Track, cover_jpeg: bytes | None = None):
    """Native M4A (AAC) downloads: iTunes-style atoms, which the H2 reads for its
    Category browser just like ID3 on MP3."""
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(audio_path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    if track.title:
        tags["\xa9nam"] = [track.title]
    if track.artist:
        tags["\xa9ART"] = [track.artist]
    if track.album:
        tags["\xa9alb"] = [track.album]
    if track.album_artist:
        tags["aART"] = [track.album_artist]
    elif primary_artist(track.artist):
        tags["aART"] = [primary_artist(track.artist)]
    if track.track_number:
        tags["trkn"] = [(int(track.track_number), int(getattr(track, "total_tracks", None) or 0))]
    if track.disc_number:
        tags["disk"] = [(int(track.disc_number), int(getattr(track, "total_discs", None) or 0))]
    if primary_genre(track.genre):
        tags["\xa9gen"] = [primary_genre(track.genre)]
    if track.year:
        tags["\xa9day"] = [str(track.year)]
    if cover_jpeg:
        tags["covr"] = [MP4Cover(cover_jpeg, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()
    logger.debug("MP4 tags applied to %s", audio_path.name)


def _picture_from_vorbis_comment(tags) -> bytes | None:
    """Decode the base64 METADATA_BLOCK_PICTURE comment that Opus/Ogg use for art."""
    import base64

    from mutagen.flac import Picture

    blocks = tags.get("metadata_block_picture") or []
    for block in blocks:
        try:
            return Picture(base64.b64decode(block)).data
        except Exception as e:
            logger.debug("Unreadable metadata_block_picture: %s", e)
    return None


async def _export_sidecar_jpg(audio_path: Path, cover_jpeg: bytes | None):
    """Save album art as a .jpg sidecar file (H2 reads this for display)."""
    jpg_path = audio_path.with_suffix(".jpg")
    if jpg_path.exists():
        return

    if cover_jpeg:
        await asyncio.to_thread(jpg_path.write_bytes, cover_jpeg)
        logger.info("Saved artwork to %s", jpg_path)
    else:
        await asyncio.to_thread(_export_embedded_art, audio_path, jpg_path)


def _export_embedded_art(audio_path: Path, jpg_path: Path):
    """Sidecar from the art already inside the file, for tracks with no artwork URL."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(audio_path)
        if audio is None:
            return

        art_data = None
        if hasattr(audio, "tags") and audio.tags:
            for key in audio.tags:
                if str(key).startswith("APIC"):
                    art_data = audio.tags[key].data
                    break
            if art_data is None and "covr" in audio.tags:
                covers = audio.tags["covr"]
                if covers:
                    art_data = bytes(covers[0])
            if art_data is None:
                art_data = _picture_from_vorbis_comment(audio.tags)
        if art_data is None and hasattr(audio, "pictures"):
            for pic in audio.pictures:
                art_data = pic.data
                break

        if art_data:
            jpeg = artwork.prepare_cover(art_data, settings.artwork_size)
            if jpeg:
                jpg_path.write_bytes(jpeg)
                logger.info("Extracted album art to %s", jpg_path)
    except Exception as e:
        logger.warning("Failed to extract album art for %s: %s", audio_path, e)
