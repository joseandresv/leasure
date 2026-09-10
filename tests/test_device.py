from pathlib import Path

import services.device as device_mod
from services.device import build_device_path, is_system_volume, resolve_sync_target, sanitize_filename


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
