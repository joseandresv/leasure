from services.playlist import (
    MANIFEST_NAME,
    generate_m3u,
    read_manifest,
    record_generated_playlists,
    sanitize_playlist_stem,
    sweep_orphan_playlists,
)

TRACKS = [{"artist": "Artist", "album": "Album", "track_number": 1, "title": "Song", "format": "mp3"}]


def test_generate_m3u_writes_bom_crlf_relative_paths(tmp_path):
    path = generate_m3u("Road: Trip?", TRACKS, tmp_path)
    assert path.name == "Road Trip.m3u8"
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\r\n")
    assert b"Artist/Album/01 - Song.mp3" in raw
    assert sanitize_playlist_stem("Road: Trip?") == path.stem


def test_sweep_never_touches_user_playlists(tmp_path):
    user_pl = tmp_path / "My Mix.m3u"
    user_pl.write_text("something")
    ours = generate_m3u("Old Leasure List", TRACKS, tmp_path)
    record_generated_playlists(tmp_path, [ours])

    removed = sweep_orphan_playlists(tmp_path, keep_names={"Some Other List"})

    assert removed == [ours.name]
    assert not ours.exists()
    assert user_pl.exists(), "a playlist Leasure did not write must survive the sweep"
    assert read_manifest(tmp_path) == set()


def test_sweep_keeps_ours_when_still_in_db(tmp_path):
    ours = generate_m3u("Keep Me", TRACKS, tmp_path)
    record_generated_playlists(tmp_path, [ours])
    assert sweep_orphan_playlists(tmp_path, keep_names={"Keep Me"}) == []
    assert ours.exists()
    assert (tmp_path / MANIFEST_NAME).exists()


def test_sweep_without_manifest_removes_nothing(tmp_path):
    (tmp_path / "stray.m3u8").write_text("x")
    assert sweep_orphan_playlists(tmp_path, keep_names=set()) == []
    assert (tmp_path / "stray.m3u8").exists()


def test_corrupt_manifest_is_treated_as_empty(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{not json")
    assert read_manifest(tmp_path) == set()
