"""Card paths stay within Rockbox's MAX_PATH (260 bytes incl. NUL): at most 250 UTF-8 bytes from the
card root, 255 per component. Paths within the limit whose folders are within their caps (every path of up
to 209 bytes; the library's longest is 199) come out byte-identical to the paths Leasure wrote before the cap
existed, so no file on an existing card is renamed. Longer folders are capped for every track of the album."""

import itertools
import re
from pathlib import Path, PurePosixPath

import pytest

from models import Track
from services.device import MAX_CARD_PATH_BYTES, build_device_path, sidecar_rel

# --- Frozen copy of build_device_path and its helpers before the byte cap (the oracle) ---------
_FAT32_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def _legacy_sanitize(name: str, max_length: int = 200) -> str:
    name = _FAT32_FORBIDDEN.sub("_", name or "")
    name = name.strip(". ")
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    return name or "Unknown"


def _legacy_artist_folder(artist, album_artist=None) -> str:
    credit = (album_artist or "").strip()
    track_artist = (artist or "").strip()
    if not credit or (credit == track_artist and "," in credit):
        credit = (track_artist or "Unknown Artist").split(",")[0].strip()
    return _legacy_sanitize(credit or "Unknown Artist")


def _legacy_prefix(track_number, disc_number=None, total_discs=None) -> str:
    if not track_number:
        return ""
    if (total_discs or 0) > 1 or (disc_number or 1) > 1:
        return f"{disc_number or 1}-{track_number:02d} - "
    return f"{track_number:02d} - "


def _legacy_build_device_path(artist, album, track_number, title, ext, album_artist=None, disc_number=None,
                              total_discs=None) -> str:
    artist_dir = _legacy_artist_folder(artist, album_artist)
    album_dir = _legacy_sanitize(album or "Unknown Album")
    filename = _legacy_sanitize(f"{_legacy_prefix(track_number, disc_number, total_discs)}{title}")
    return f"{artist_dir}/{album_dir}/{filename}.{ext}"
# ------------------------------------------------------------------------------------------------


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _assert_valid(rel: str) -> None:
    assert _b("/" + rel) <= MAX_CARD_PATH_BYTES
    assert rel.encode("utf-8").decode("utf-8") == rel
    for part in rel.split("/"):
        assert part and _b(part) <= 255
        assert not part.endswith((".", " "))


ARTISTS = [("The Killers", None), ("Earth, Wind & Fire", None), ("Daft Punk, Pharrell Williams", "Daft Punk"),
           ("아이유", None), ("방탄소년단", None), ("米津玄師", None), ("宇多田ヒカル", None), ("きゃりーぱみゅぱみゅ", None),
           ("CON", None), ('AC/DC: "Live"?', None), ("Sigur Rós 🎵", None), ("", None), ("A" * 200, None)]
ALBUMS = ["Hot Fuss", "Palette", "STRAY SHEEP", "BADモード", "Love Yourself 轉 'Tear'", "", "Album." , "B" * 200]
TITLES = ["Mr. Brightside", "이름에게", "感電", "Pink Blood", "Song (feat. Someone) [Remix]", "Track...", "Ω" * 70,
          "x" * 150 + ". . .", "C" * 200, "노래제목" * 30, "😀" * 60]
NUMBERS = [(None, None, None), (2, None, None), (1, 1, 2), (12, 2, 2), (7, 3, None)]
EXTS = ["m4a", "flac", "mp3", "opus", "jpg", "lrc"]


def _corpus():
    for (artist, album_artist), album, title, (n, disc, total), ext in itertools.product(
            ARTISTS, ALBUMS, TITLES, NUMBERS, EXTS):
        yield (artist, album, n, title, ext), {"album_artist": album_artist, "disc_number": disc, "total_discs": total}


def _folders_within_caps(rel: str) -> bool:
    artist, album, _ = rel.split("/")
    return _b(artist) <= 200 and _b(artist) + _b(album) <= 210


def test_paths_within_the_limit_are_byte_identical_to_the_legacy_ones():
    checked = 0
    for args, kwargs in _corpus():
        legacy = _legacy_build_device_path(*args, **kwargs)
        if _b("/" + legacy) <= MAX_CARD_PATH_BYTES and _folders_within_caps(legacy):
            assert build_device_path(*args, **kwargs) == legacy
            checked += 1
    assert checked > 1000


def test_every_path_up_to_209_bytes_is_byte_identical_to_the_legacy_one():
    checked = 0
    for args, kwargs in _corpus():
        legacy = _legacy_build_device_path(*args, **kwargs)
        if _b("/" + legacy) <= 209:
            assert build_device_path(*args, **kwargs) == legacy
            checked += 1
    assert checked > 1000


def test_the_shortest_path_with_a_capped_folder_is_210_bytes():
    legacy = _legacy_build_device_path("가" * 67, "X", None, "a", "mp3")
    assert _b("/" + legacy) == 210
    assert build_device_path("가" * 67, "X", None, "a", "mp3") != legacy


def test_every_path_fits_rockbox_whatever_its_names():
    for args, kwargs in _corpus():
        _assert_valid(build_device_path(*args, **kwargs))


@pytest.mark.parametrize("args, kwargs, expected", [
    (("The Killers", "Hot Fuss", 2, "Mr. Brightside", "m4a"), {}, "The Killers/Hot Fuss/02 - Mr. Brightside.m4a"),
    (("아이유", "Palette", 3, "이름에게", "m4a"), {}, "아이유/Palette/03 - 이름에게.m4a"),
    (("米津玄師", "STRAY SHEEP", 5, "感電", "flac"), {}, "米津玄師/STRAY SHEEP/05 - 感電.flac"),
    (("宇多田ヒカル", "BADモード", 1, "BADモード", "m4a"), {"disc_number": 1, "total_discs": 2},
     "宇多田ヒカル/BADモード/1-01 - BADモード.m4a"),
])
def test_ordinary_names_keep_todays_paths(args, kwargs, expected):
    assert build_device_path(*args, **kwargs) == expected == _legacy_build_device_path(*args, **kwargs)


@pytest.mark.parametrize("artist, album", [
    ("O" * 61, "Sinfonía n.º 9 (Remasterizado 2024) Vol. " + "B" * 117),
    ("宇多田ヒカル" * 5, "オリジナル・サウンドトラック" * 3),
    ("A" * 100, "B" * 120),
    ("가" * 70, "Map"),
    ("IU", "앨" * 70),
])
def test_every_track_of_an_album_with_long_folders_shares_one_folder(artist, album):
    short = build_device_path(artist, album, 1, "Allegro", "flac")
    long = build_device_path(artist, album, 2, "Adagio molto e cantabile " * 4, "flac")
    _assert_valid(short)
    _assert_valid(long)
    assert PurePosixPath(short).parent == PurePosixPath(long).parent
    assert PurePosixPath(short).name == "01 - Allegro.flac"


@pytest.mark.parametrize("title_len, legacy_bytes", [(156, 248), (157, 249), (158, 250)])
def test_paths_up_to_250_bytes_are_unchanged(title_len, legacy_bytes):
    args = ("A" * 40, "B" * 40, 1, "t" * title_len, "mp3")
    legacy = _legacy_build_device_path(*args)
    assert _b("/" + legacy) == legacy_bytes
    assert build_device_path(*args) == legacy


def test_a_251_byte_path_is_trimmed_to_fit():
    rel = build_device_path("A" * 40, "B" * 40, 1, "t" * 159, "mp3")
    _assert_valid(rel)
    assert rel.startswith(f"{'A' * 40}/{'B' * 40}/01 - ttt") and re.search(r"~[0-9a-f]{6}\.mp3$", rel)


def test_a_trimmed_title_keeps_its_prefix_extension_and_character_boundaries():
    rel = build_device_path("아이유", "앨범" * 60, 12, "노래제목" * 30, "m4a", disc_number=2, total_discs=2)
    _assert_valid(rel)
    artist, album, name = rel.split("/")
    assert artist == "아이유"
    assert re.fullmatch(r"(앨범)+앨?~[0-9a-f]{6}", album)
    assert name.startswith("2-12 - 노래") and name.endswith(".m4a")


def test_trimming_is_deterministic_and_keeps_distinct_titles_apart():
    common = "感" * 80
    first = build_device_path("米津玄師", "STRAY SHEEP", 5, common + "X", "m4a")
    assert first == build_device_path("米津玄師", "STRAY SHEEP", 5, common + "X", "m4a")
    assert first != build_device_path("米津玄師", "STRAY SHEEP", 5, common + "Y", "m4a")


def test_long_and_short_titles_of_one_album_share_its_folder():
    album = "앨범" * 60
    short = build_device_path("아이유", album, 1, "Intro", "m4a")
    long = build_device_path("아이유", album, 2, "노래제목" * 30, "m4a")
    assert PurePosixPath(short).parent == PurePosixPath(long).parent


def test_a_trimmed_stem_is_the_same_for_every_audio_extension():
    stems = {build_device_path("米津玄師", "STRAY SHEEP", 5, "感" * 80, ext).rsplit(".", 1)[0]
             for ext in ("m4a", "flac", "opus")}
    assert len(stems) == 1


def test_an_oversized_artist_folder_is_capped_at_200_bytes():
    rel = build_device_path("영" * 100, "Album", 1, "Song", "mp3")
    _assert_valid(rel)
    assert _b(rel.split("/")[0]) <= 200


def test_sidecar_rel_keeps_the_audio_stem():
    assert sidecar_rel("Artist/Album/05 - Mr. Brightside.m4a", ".jpg") == "Artist/Album/05 - Mr. Brightside.jpg"


def test_sidecars_follow_the_trimmed_audio_name(tmp_path):
    """A .flac path of 251 bytes is trimmed while the same name as .jpg (250 bytes) would not be."""
    from routers.device import _track_files

    title = "t" * 158
    assert _b("/" + build_device_path("A" * 40, "B" * 40, 1, title, "jpg")) == 250
    src = tmp_path / "song.flac"
    src.write_bytes(b"fLaC")
    src.with_suffix(".jpg").write_bytes(b"jpg")
    src.with_suffix(".lrc").write_bytes(b"lrc")
    track = Track(title=title, artist="A" * 40, album="B" * 40, track_number=1)

    files = _track_files(src, track)

    audio_stem = files[0][2].rsplit(".", 1)[0]
    assert "~" in audio_stem
    assert [rel for _, _, rel in files[1:]] == [audio_stem + ".jpg", audio_stem + ".lrc"]


def test_the_windows_path_precheck_never_fires_for_a_capped_path(tmp_path, monkeypatch):
    from routers import device

    monkeypatch.setattr(device, "get_platform", lambda: "wsl2")
    src = tmp_path / "x.m4a"
    src.write_bytes(b"x" * 10)
    track = Track(id=1, title="노래" * 100, artist="아이유" * 30, album="앨범" * 60, track_number=1,
                  file_path=str(src))
    card = tmp_path / "card"
    card.mkdir()

    plan = device._plan_space(Path(card), [track])

    assert plan.too_long == [] and plan.skip == {}
