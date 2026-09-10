import shutil
import struct
import subprocess

import pytest

from models import Track
from services.tagger import (
    _embed_vorbis,
    _export_sidecar_jpg,
    _tag_flac,
    _tag_mp3,
    _tag_mp4,
    primary_artist,
    primary_genre,
)


def _track(**overrides) -> Track:
    base = dict(title="Get Lucky", artist="Daft Punk, Pharrell Williams", album="RAM",
                genre="electronic, disco, funk", track_number=8, disc_number=1, year=2013)
    base.update(overrides)
    return Track(**base)


def test_primary_genre_takes_first_of_list():
    assert primary_genre("rock, alternative rock, indie") == "rock"
    assert primary_genre(" jazz ") == "jazz"
    assert primary_genre("") is None
    assert primary_genre(None) is None
    assert primary_genre(", x") is None


def test_primary_artist():
    assert primary_artist("Daft Punk, Pharrell Williams") == "Daft Punk"
    assert primary_artist(None) is None


def _write_minimal_mp3(path):
    # One MPEG-1 Layer III frame, 128 kbps, 44.1 kHz, no CRC: 417 bytes; write a few.
    header = b"\xff\xfb\x90\x00"
    frame = header + b"\x00" * (417 - 4)
    path.write_bytes(frame * 4)


def _write_minimal_flac(path):
    streaminfo = struct.pack(">HH", 1024, 4096) + b"\x00" * 6
    # sample rate 44100 (20 bits), channels-1 (3 bits), bps-1 (5 bits), total samples (36 bits)
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    streaminfo += packed.to_bytes(8, "big") + b"\x00" * 16
    block_header = bytes([0x80 | 0]) + len(streaminfo).to_bytes(3, "big")
    path.write_bytes(b"fLaC" + block_header + streaminfo)


def test_tag_mp3_writes_single_genre_and_album_artist_default(tmp_path):
    from mutagen.id3 import ID3

    f = tmp_path / "t.mp3"
    _write_minimal_mp3(f)
    _tag_mp3(f, _track(album_artist=None))
    tags = ID3(f)
    assert tags["TCON"].text == ["electronic"]
    assert tags["TPE2"].text == ["Daft Punk"]
    assert tags["TPE1"].text == ["Daft Punk, Pharrell Williams"]
    assert tags["TRCK"].text == ["8"]
    assert tags["TDRC"].text[0].text == "2013"


def test_tag_flac_uses_vorbis_keys(tmp_path):
    from mutagen.flac import FLAC

    f = tmp_path / "t.flac"
    _write_minimal_flac(f)
    _tag_flac(f, _track(album_artist="Daft Punk"))
    audio = FLAC(f)
    assert audio["genre"] == ["electronic"]
    assert audio["albumartist"] == ["Daft Punk"]
    assert audio["tracknumber"] == ["8"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed to synthesize an M4A")
def test_tag_mp4_writes_itunes_atoms(tmp_path):
    from mutagen.mp4 import MP4

    f = tmp_path / "t.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "0.2", "-c:a", "aac", str(f)], check=True)
    _tag_mp4(f, _track(album_artist=None))
    tags = MP4(f).tags
    assert tags["\xa9nam"] == ["Get Lucky"]
    assert tags["aART"] == ["Daft Punk"]
    assert tags["\xa9gen"] == ["electronic"]
    assert tags["trkn"] == [(8, 0)]
    assert tags["\xa9day"] == ["2013"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed to synthesize an Opus file")
async def test_sidecar_jpg_extracted_from_opus_picture_comment(tmp_path):
    import io

    from PIL import Image

    f = tmp_path / "t.opus"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                    "-t", "0.2", "-c:a", "libopus", str(f)], check=True)
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, "JPEG")
    _embed_vorbis(f, buf.getvalue())

    await _export_sidecar_jpg(f, artwork_url=None)

    jpg = f.with_suffix(".jpg")
    assert jpg.exists()
    assert Image.open(jpg).format == "JPEG"
