from pathlib import Path

import pytest

import services.device as device_mod
from config import settings
from services.device import build_device_path, is_system_volume, resolve_sync_target, sanitize_filename, track_prefix


def test_sanitize_strips_fat32_forbidden_and_control_chars():
    assert sanitize_filename('AC/DC: "Back" <in> Black?*|') == "AC_DC_ _Back_ _in_ Black___"
    assert sanitize_filename("tab\there\x00") == "tab_here_"


def test_sanitize_reserved_names_and_empty():
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("nul.mp3") == "_nul.mp3"
    assert sanitize_filename("   ...  ") == "Unknown"
    assert sanitize_filename("") == "Unknown"


def test_sanitize_truncates_without_trailing_dot():
    long = "a" * 250 + "."
    out = sanitize_filename(long, max_length=100)
    assert len(out) == 100 and not out.endswith(".")


def test_build_device_path_is_posix_and_uses_primary_artist():
    rel = build_device_path("Daft Punk, Pharrell Williams", "Random Access Memories", 8, "Get Lucky", "mp3")
    assert rel == "Daft Punk/Random Access Memories/08 - Get Lucky.mp3"
    assert "\\" not in rel


def test_build_device_path_prefers_album_artist_over_comma_split():
    rel = build_device_path("Earth, Wind & Fire, Ramsey Lewis", "Gratitude", 3, "Sing a Song", "mp3",
                            album_artist="Earth, Wind & Fire")
    assert rel == "Earth, Wind & Fire/Gratitude/03 - Sing a Song.mp3"


def test_build_device_path_splits_a_pre_sprint2_album_artist_copied_from_the_artist():
    """Old rows stored the whole track artist as album_artist; splitting keeps their folders."""
    rel = build_device_path("Calvin Harris, Rihanna", "18 Months", 4, "We Found Love", "mp3",
                            album_artist="Calvin Harris, Rihanna")
    assert rel == "Calvin Harris/18 Months/04 - We Found Love.mp3"


def test_build_device_path_falls_back_to_comma_split_without_album_artist():
    rel = build_device_path("Earth, Wind & Fire", "Gratitude", 3, "Sing a Song", "mp3")
    assert rel == "Earth/Gratitude/03 - Sing a Song.mp3"


def test_build_device_path_without_track_number():
    assert build_device_path(None, None, None, "Untitled", "flac") == "Unknown Artist/Unknown Album/Untitled.flac"


def test_is_system_volume_marks_os_roots():
    assert is_system_volume(["Windows", "Users", "Program Files"])
    assert is_system_volume(["Windows"])
    assert is_system_volume(["Program Files", "Users"])
    # A card with a Users folder but no OS is still a valid sync target
    assert not is_system_volume(["Users"])
    assert not is_system_volume(["Daft Punk", "PLAYLIST", "System Volume Information"])


def test_classify_small_system_drive_is_never_a_player(tmp_path):
    (tmp_path / "Windows").mkdir()
    (tmp_path / "Users").mkdir()
    info = device_mod._classify(str(tmp_path), "C:", "C", removable_hint=True)
    assert info["device_type"] == "system"


def test_resolve_sync_target_only_accepts_detected_non_system_volumes(tmp_path, monkeypatch):
    player = tmp_path / "H2"
    player.mkdir()
    system = tmp_path / "C"
    system.mkdir()
    fake = [
        {"path": str(player), "device_type": "player", "label": "H2"},
        {"path": str(system), "device_type": "system", "label": "C:"},
    ]
    monkeypatch.setattr(device_mod, "detect_devices", lambda: fake)

    assert resolve_sync_target(str(player)) == str(player)
    # Same volume spelled differently still resolves
    assert resolve_sync_target(str(player) + "/") == str(player)
    assert resolve_sync_target(str(system)) is None
    assert resolve_sync_target(str(tmp_path / "elsewhere")) is None
    assert resolve_sync_target("") is None
    assert resolve_sync_target(str(Path.home())) is None


def test_library_stem_matches_the_device_path_for_the_same_row(monkeypatch, tmp_path):
    from services.yt_engine import _library_stem

    monkeypatch.setattr(settings, "library_dir", tmp_path)
    row = ("Calvin Harris, Rihanna", "18 Months", "We Found Love", 4)
    for album_artist in (None, "Calvin Harris, Rihanna", "Calvin Harris"):
        for disc_number, total_discs in ((None, None), (1, 1), (2, 2), (1, 2)):
            stem = _library_stem(*row, album_artist, disc_number, total_discs)
            rel = build_device_path(row[0], row[1], row[3], row[2], "mp3", album_artist, disc_number, total_discs)
            assert f"{stem.relative_to(tmp_path).as_posix()}.mp3" == rel


@pytest.mark.parametrize(("args", "prefix"), [
    ((None,), ""),
    ((3,), "03 - "),
    ((3, 1, 1), "03 - "),
    ((3, 1, 2), "1-03 - "),
    ((1, 2, 2), "2-01 - "),
    ((1, 2, None), "2-01 - "),
])
def test_track_prefix(args, prefix):
    assert track_prefix(*args) == prefix


def test_a_two_disc_album_sorts_disc_one_before_disc_two():
    tracks = [(2, 1), (1, 3), (2, 2), (1, 1), (1, 2)]
    paths = [build_device_path("Daft Punk", "RAM", n, f"Song {d}{n}", "mp3", disc_number=d, total_discs=2)
             for d, n in tracks]

    names = [p.rsplit("/", 1)[1] for p in sorted(paths)]

    assert names == ["1-01 - Song 11.mp3", "1-02 - Song 12.mp3", "1-03 - Song 13.mp3",
                     "2-01 - Song 21.mp3", "2-02 - Song 22.mp3"]
