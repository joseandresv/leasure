import io
import shutil
import subprocess

import pytest
from PIL import Image

from config import settings
from models import Track
from services import artwork
from services.tagger import (
    _export_sidecar_jpg,
    _tag_flac,
    _tag_mp3,
    _tag_mp4,
    _tag_vorbis,
    _write_tags,
    apply_full_metadata,
    primary_artist,
    primary_genre,
)

FFMPEG_INPUT = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.3"]
CODECS = {"mp3": "libmp3lame", "flac": "flac", "m4a": "aac", "opus": "libopus"}


@pytest.fixture(scope="session")
def media_originals(tmp_path_factory):
    """One real file per container, synthesised once with ffmpeg."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg needed to synthesize audio files")
    root = tmp_path_factory.mktemp("media")
    originals = {}
    for suffix, codec in CODECS.items():
        path = root / f"sample.{suffix}"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *FFMPEG_INPUT, "-c:a", codec, str(path)], check=True)
        originals[suffix] = path
    return originals


@pytest.fixture
def media_file(media_originals, tmp_path):
    def _copy(suffix: str):
        target = tmp_path / f"track.{suffix}"
        shutil.copyfile(media_originals[suffix], target)
        return target

    return _copy


def _jpeg(width: int, height: int, colour: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def cover_jpeg() -> bytes:
    return artwork.prepare_cover(_jpeg(600, 600))


def _track(**overrides) -> Track:
    # The totals are set after construction so the test works whether or not the
    # total_tracks / total_discs columns have landed.
    totals = {key: overrides.pop(key, None) for key in ("total_tracks", "total_discs")}
    base = dict(title="Get Lucky", artist="Daft Punk, Pharrell Williams", album="RAM",
                genre="electronic, disco, funk", track_number=8, disc_number=1, year=2013)
    base.update(overrides)
    track = Track(**base)
    for key, value in totals.items():
        setattr(track, key, value)
    return track


def test_primary_genre_takes_first_of_list():
    assert primary_genre("rock, alternative rock, indie") == "rock"
    assert primary_genre(" jazz ") == "jazz"
    assert primary_genre("") is None
    assert primary_genre(None) is None
    assert primary_genre(", x") is None


def test_primary_artist():
    assert primary_artist("Daft Punk, Pharrell Williams") == "Daft Punk"
    assert primary_artist(None) is None


def test_tag_mp3_writes_single_genre_and_album_artist_default(media_file):
    from mutagen.id3 import ID3

    f = media_file("mp3")
    _tag_mp3(f, _track(album_artist=None))
    tags = ID3(f)
    assert tags["TCON"].text == ["electronic"]
    assert tags["TPE2"].text == ["Daft Punk"]
    assert tags["TPE1"].text == ["Daft Punk, Pharrell Williams"]
    assert tags["TRCK"].text == ["8"]


def test_tag_flac_uses_vorbis_keys(media_file):
    from mutagen.flac import FLAC

    f = media_file("flac")
    _tag_flac(f, _track(album_artist="Daft Punk"))
    audio = FLAC(f)
    assert audio["genre"] == ["electronic"]
    assert audio["albumartist"] == ["Daft Punk"]
    assert audio["tracknumber"] == ["8"]


def test_tag_mp4_writes_itunes_atoms(media_file):
    from mutagen.mp4 import MP4

    f = media_file("m4a")
    _tag_mp4(f, _track(album_artist=None))
    tags = MP4(f).tags
    assert tags["\xa9nam"] == ["Get Lucky"]
    assert tags["aART"] == ["Daft Punk"]
    assert tags["\xa9gen"] == ["electronic"]
    assert tags["\xa9day"] == ["2013"]


def test_mp3_tags_and_cover_land_in_one_save(media_file, cover_jpeg, monkeypatch):
    from mutagen.id3 import ID3

    saves = []
    original_save = ID3.save

    def counting_save(self, *args, **kwargs):
        saves.append(1)
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(ID3, "save", counting_save)
    f = media_file("mp3")
    _write_tags(f, _track(), cover_jpeg)

    tags = ID3(f)
    assert saves == [1]
    assert tags["TIT2"].text == ["Get Lucky"]
    assert tags.getall("APIC")[0].data == cover_jpeg


def test_retagging_without_a_fetched_cover_keeps_the_embedded_art(media_file, cover_jpeg):
    """An offline re-tag has no cover_jpeg; stripping APIC would lose the art for good."""
    from mutagen.id3 import ID3

    f = media_file("mp3")
    _write_tags(f, _track(), cover_jpeg)
    _write_tags(f, _track(title="Get Lucky - Remaster"), None)

    tags = ID3(f)
    assert tags["TIT2"].text == ["Get Lucky - Remaster"]
    assert tags.getall("APIC")[0].data == cover_jpeg


def test_mp3_writes_track_and_disc_totals(media_file):
    from mutagen.id3 import ID3

    f = media_file("mp3")
    _tag_mp3(f, _track(track_number=3, total_tracks=12, disc_number=1, total_discs=2))
    tags = ID3(f)
    assert tags["TRCK"].text == ["3/12"]
    assert tags["TPOS"].text == ["1/2"]


def test_mp3_id3v23_writes_tyer_as_utf16(media_file, monkeypatch):
    from mutagen.id3 import ID3

    monkeypatch.setattr(settings, "id3_version", 3)
    f = media_file("mp3")
    _tag_mp3(f, _track())

    tags = ID3(f, translate=False)
    assert "TYER" in tags
    assert "TDRC" not in tags
    assert tags["TYER"].text == ["2013"]
    assert tags["TIT2"].encoding == 1


def test_mp3_id3v24_keeps_tdrc(media_file, monkeypatch):
    from mutagen.id3 import ID3

    monkeypatch.setattr(settings, "id3_version", 4)
    f = media_file("mp3")
    _tag_mp3(f, _track())

    tags = ID3(f, translate=False)
    assert "TDRC" in tags
    assert "TYER" not in tags
    assert tags["TIT2"].encoding == 3


def test_flac_totals_use_separate_keys(media_file):
    from mutagen.flac import FLAC

    f = media_file("flac")
    _tag_flac(f, _track(track_number=3, total_tracks=12, disc_number=1, total_discs=2))
    audio = FLAC(f)
    assert audio["tracknumber"] == ["3"]
    assert audio["tracktotal"] == ["12"]
    assert audio["disctotal"] == ["2"]


def test_mp4_totals_go_in_the_number_pair(media_file):
    from mutagen.mp4 import MP4

    f = media_file("m4a")
    _tag_mp4(f, _track(track_number=3, total_tracks=12, disc_number=1, total_discs=2))
    tags = MP4(f).tags
    assert tags["trkn"] == [(3, 12)]
    assert tags["disk"] == [(1, 2)]


def test_mp4_cover_written_with_the_tags(media_file, cover_jpeg):
    from mutagen.mp4 import MP4

    f = media_file("m4a")
    _write_tags(f, _track(), cover_jpeg)
    tags = MP4(f).tags
    assert tags["\xa9nam"] == ["Get Lucky"]
    assert bytes(tags["covr"][0]) == cover_jpeg


def test_prepare_cover_crops_to_square_without_upscaling():
    jpeg = artwork.prepare_cover(_jpeg(300, 200))
    assert Image.open(io.BytesIO(jpeg)).size == (200, 200)


def test_prepare_cover_downscales_large_art():
    jpeg = artwork.prepare_cover(_jpeg(1000, 1000))
    assert Image.open(io.BytesIO(jpeg)).size == (settings.artwork_size, settings.artwork_size)


def test_prepare_cover_rejects_data_that_is_not_an_image():
    assert artwork.prepare_cover(b"not an image") is None


async def test_sidecar_jpg_extracted_from_opus_picture_comment(media_file):
    f = media_file("opus")
    _tag_vorbis(f, _track(), artwork.prepare_cover(_jpeg(64, 64)))

    await _export_sidecar_jpg(f, None)

    jpg = f.with_suffix(".jpg")
    assert jpg.exists()
    assert Image.open(jpg).format == "JPEG"


async def test_full_metadata_downloads_the_artwork_once(db, media_file, monkeypatch):
    from mutagen.id3 import ID3

    from services import lyrics

    calls = []

    async def _fetch(url):
        calls.append(url)
        return _jpeg(800, 800)

    async def _no_lyrics(*args, **kwargs):
        return None

    monkeypatch.setattr(artwork, "fetch_artwork", _fetch)
    monkeypatch.setattr(lyrics, "save_lrc", _no_lyrics)

    async with db() as session:
        track = _track(genre="electronic", artwork_url="https://i.scdn.co/image/abc")
        session.add(track)
        await session.commit()
        track_id = track.id

    f = media_file("mp3")
    await apply_full_metadata(f, track_id)

    assert calls == ["https://i.scdn.co/image/abc"]
    sidecar = f.with_suffix(".jpg")
    assert sidecar.exists()
    assert ID3(f).getall("APIC")[0].data == sidecar.read_bytes()


async def _refuse_fetch(url):
    raise AssertionError(f"fetched {url}")


async def test_a_siblings_sidecar_url_is_read_from_disk(db, monkeypatch):
    from services.tagger import _prepare_cover

    folder = settings.library_dir / "tagger-sidecar"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01 - Song.jpg").write_bytes(_jpeg(64, 64))
    monkeypatch.setattr(artwork, "fetch_artwork", _refuse_fetch)
    async with db() as session:
        track = _track(file_path=str(folder / "01 - Song.m4a"))
        session.add(track)
        await session.commit()

    try:
        cover = await _prepare_cover(f"/api/library/art/{track.id}")
    finally:
        shutil.rmtree(folder)

    assert cover and cover[:2] == b"\xff\xd8"


async def test_an_album_cover_url_is_read_from_the_cover_cache(monkeypatch):
    from services.tagger import _prepare_cover

    key = "a-" + "1" * 20
    path = artwork.album_cover_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_jpeg(64, 64))
    monkeypatch.setattr(artwork, "fetch_artwork", _refuse_fetch)

    try:
        assert await _prepare_cover(f"/api/albums/{key}/cover") is not None
        assert await _prepare_cover(f"/api/albums/{'a-' + '2' * 20}/cover") is None
    finally:
        path.unlink()
