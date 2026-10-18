import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from config import settings
from models import Playlist, PlaylistTrack, Track
from services import card_import
from services.card_import import CardFile, apply_match, dedupe_album, infer_total_discs, match_album

FFMPEG_INPUT = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1"]


@pytest.fixture(scope="session")
def originals(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg needed to synthesize audio files")
    root = tmp_path_factory.mktemp("card-media")
    made = {}
    for suffix, codec in (("mp3", "libmp3lame"), ("m4a", "aac")):
        path = root / f"sample.{suffix}"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *FFMPEG_INPUT, "-c:a", codec, "-b:a", "128k", str(path)],
                       check=True)
        made[suffix] = path
    return made


@pytest.fixture
def card(tmp_path, originals, monkeypatch, isolated_paths):
    """A fake card root plus a helper that puts a tagged audio file (and sidecars) on it."""
    root = tmp_path / "card"
    root.mkdir()
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    isolated_paths()

    def put(rel: str, sidecars=(".jpg",), **tags) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(originals[path.suffix.lstrip(".")], path)
        _write_tags(path, tags)
        for suffix in sidecars:
            path.with_suffix(suffix).write_bytes(f"{rel}{suffix}".encode())
        return path

    put.root = root
    return put


def _write_tags(path: Path, tags: dict) -> None:
    if path.suffix == ".mp3":
        from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TRCK

        id3 = ID3()
        frames = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2, "track": TRCK}
        for key, frame in frames.items():
            if key in tags:
                id3.add(frame(encoding=3, text=str(tags[key])))
        id3.save(path)
    else:
        from mutagen.mp4 import MP4

        audio = MP4(path)
        atoms = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "album_artist": "aART"}
        for key, atom in atoms.items():
            if key in tags:
                audio[atom] = [tags[key]]
        audio.save()


def _album(name: str, tracks: list[tuple], provider: str = "spotify", total_discs: int = 1) -> dict:
    return {"provider": provider, "id": f"{provider}-{name}", "name": name, "artist": "Artist", "artwork_url": None,
            "total_tracks": len(tracks), "total_discs": total_discs,
            "tracks": [{"title": title, "duration_ms": duration, "track_number": number, "disc_number": disc,
                        "isrc": None, "spotify_uri": f"spotify:track:{title}" if provider == "spotify" else None,
                        "youtube_id": f"yt-{title}" if provider == "youtube" else None}
                       for title, duration, number, disc in tracks]}


@pytest.fixture
def providers(monkeypatch):
    """Spotify / YouTube Music answers per album name, and the calls made."""
    answers = {"spotify": {}, "youtube": {}}
    calls = []

    def fake(provider):
        def lookup(artist, album):
            calls.append((provider, artist, album))
            return answers[provider].get(album, [])
        return lookup

    for provider in answers:
        monkeypatch.setitem(card_import.PROVIDERS, provider, fake(provider))
    answers["calls"] = calls
    return answers


def _cf(rel: str, title: str, duration_ms: int = 200_000, **fields) -> CardFile:
    return CardFile(rel=rel, path=Path("/card") / rel, size=fields.pop("size", 1000), mtime=0.0, title=title,
                    artist="Artist", album="Album", album_artist="Artist", duration_ms=duration_ms, **fields)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _run(card, tmp_path, **kwargs):
    return await card_import.import_card(card.root, tmp_path / "report" / "report.json", **kwargs)


async def _tracks(db) -> list[Track]:
    async with db() as session:
        return list((await session.scalars(select(Track).order_by(Track.id))).all())


def test_a_file_without_tags_takes_its_names_from_the_folders_and_the_number_from_the_prefix(card):
    path = card("Artist/Album/03 - Song.mp3")

    f = card_import.read_card_file(card.root, path)

    assert (f.artist, f.album, f.title, f.track_number) == ("Artist", "Album", "Song", 3)


def test_unknown_album_folder_is_not_an_album_name(card):
    f = card_import.read_card_file(card.root, card("Artist/Unknown Album/Song.mp3", title="Song", artist="Artist"))

    assert f.album is None


def test_dedupe_keeps_the_numbered_copy_of_a_song_at_the_same_bitrate():
    numbered = _cf("A/Album/01 - Perfect Night.mp3", "Perfect Night", track_number=1, bitrate_kbps=320)
    plain = _cf("A/Album/Perfect Night.mp3", "Perfect Night", bitrate_kbps=320, size=5000)

    kept, dropped = dedupe_album([plain, numbered])

    assert kept == [numbered] and dropped == [(plain, numbered)]


def test_dedupe_keeps_the_higher_bitrate_copy_with_the_dropped_copys_numbers():
    numbered = _cf("f/SEQUENCE 01/04 - UFO.m4a", "UFO", track_number=4, disc_number=1, bitrate_kbps=128)
    plain = _cf("f/SEQUENCE 01/UFO.mp3", "UFO", bitrate_kbps=320, size=5000)

    kept, dropped = dedupe_album([numbered, plain])

    assert kept == [plain] and dropped == [(numbered, plain)]
    assert (plain.track_number, plain.disc_number) == (4, 1)


def test_dedupe_treats_a_featured_artist_credit_as_the_same_title():
    a = _cf("A/Album/Sugar Free Venom feat. Kesha (feat. Kesha).mp3", "Sugar Free Venom feat. Kesha (feat. Kesha)")
    b = _cf("A/Album/07 - Sugar Free Venom feat. Kesha.m4a", "Sugar Free Venom feat. Kesha", track_number=7)

    kept, _ = dedupe_album([a, b])

    assert kept == [b]


def test_dedupe_keeps_two_recordings_that_share_a_title_but_not_a_length():
    kept, dropped = dedupe_album([_cf("A/Album/Touch.mp3", "Touch", 200_000),
                                  _cf("A/Album/Touch (Live).mp3", "Touch", 300_000)])

    assert len(kept) == 2 and dropped == []


def test_a_disc_two_makes_every_file_of_the_folder_multi_disc():
    files = [_cf("D/RAM/01 - A.mp3", "A", track_number=1, disc_number=1),
             _cf("D/RAM/01 - B.mp3", "B", track_number=1, disc_number=2)]

    infer_total_discs(files)

    assert [f.total_discs for f in files] == [2, 2]


def test_a_confident_match_fills_missing_numbers_and_ids_only():
    files = [_cf("A/Album/Song One.mp3", "Song One"),
             _cf("A/Album/02 - Song Two.mp3", "Song Two", track_number=2)]
    match = match_album(files, "Album", _album("Album", [("Song One", 201_000, 1, 1), ("Song Two", 199_000, 2, 1)]))

    conflicts = apply_match(files, match)

    assert match.confident and conflicts == []
    assert [(f.track_number, f.disc_number, f.total_tracks) for f in files] == [(1, 1, 2), (2, 1, 2)]
    assert files[0].spotify_uri == "spotify:track:Song One"


def test_a_number_the_lookup_disagrees_with_is_reported_not_changed():
    files = [_cf("A/Album/05 - Song.mp3", "Song", track_number=5)]
    match = match_album(files, "Album", _album("Album", [("Song", 200_000, 3, 1)]))

    conflicts = apply_match(files, match)

    assert files[0].track_number == 5
    assert conflicts == [{"card": "A/Album/05 - Song.mp3", "field": "track_number", "card_value": 5,
                          "lookup_value": 3, "provider": "spotify"}]


def test_titles_that_match_but_lengths_that_do_not_are_no_match():
    files = [_cf("A/Album/Song.mp3", "Song", 200_000)]

    match = match_album(files, "Album", _album("Album", [("Song", 260_000, 1, 1)]))

    assert not match.confident and match.pairs == []


def test_a_long_spotify_retry_after_ends_spotify_lookups_instead_of_sleeping(monkeypatch):
    import spotipy

    naps = []
    monkeypatch.setattr(card_import.time, "sleep", naps.append)

    def throttled(**_):
        raise spotipy.SpotifyException(429, -1, "slow down", headers={"Retry-After": "3600"})

    with pytest.raises(card_import.ProviderThrottled):
        card_import._spotify_call(throttled, q="x")
    assert max(naps) < 3600


@pytest.mark.asyncio
async def test_a_throttled_provider_is_skipped_for_the_rest_of_the_run(db, card, tmp_path, providers, monkeypatch):
    def throttled(artist, album):
        providers["calls"].append(("spotify", artist, album))
        raise card_import.ProviderThrottled("spotify asked to wait 3600 s")

    monkeypatch.setitem(card_import.PROVIDERS, "spotify", throttled)
    card("Artist/One/Song.mp3", title="Song", artist="Artist", album="One")
    card("Artist/Two/Song.mp3", title="Song", artist="Artist", album="Two")

    report = await _run(card, tmp_path, dry_run=True)

    assert [c[0] for c in providers["calls"]] == ["spotify", "youtube", "youtube"]
    assert "spotify" in report.lookup_disabled


@pytest.mark.asyncio
async def test_import_copies_renames_and_retags_and_never_touches_the_card(db, card, tmp_path, providers):
    from mutagen.id3 import ID3

    src = card("Artist/Album/Song One.mp3", sidecars=(".jpg", ".lrc"), title="Song One", artist="Artist",
               album="Album", album_artist="Artist")
    providers["spotify"]["Album"] = [_album("Album", [("Song One", 1000, 4, 1)])]
    before = {p: _sha(p) for p in card.root.rglob("*") if p.is_file()}

    report = await _run(card, tmp_path)

    dest = settings.library_dir / "Artist" / "Album" / "04 - Song One.mp3"
    assert {p: _sha(p) for p in card.root.rglob("*") if p.is_file()} == before
    assert dest.exists() and dest.with_suffix(".lrc").read_bytes() == src.with_suffix(".lrc").read_bytes()
    assert str(ID3(dest)["TRCK"]) == "4/1"
    [track] = await _tracks(db)
    assert (track.source, track.status, track.verification, track.file_path) == ("card", "done", "imported", str(dest))
    assert (track.track_number, track.spotify_uri, track.premium_used, track.synced_at) == (
        4, "spotify:track:Song One", None, None)
    assert report.lists["stale_card_copies"] == [{"card": "Artist/Album/Song One.mp3",
                                                  "replaced_by": "Artist/Album/04 - Song One.mp3"}]


@pytest.mark.asyncio
async def test_an_unchanged_file_already_at_its_card_path_counts_as_synced(db, card, tmp_path, providers):
    import os

    src = card("Artist/Album/01 - Song.m4a", title="Song", artist="Artist", album="Album", album_artist="Artist")
    from mutagen.mp4 import MP4

    audio = MP4(src)
    audio["trkn"] = [(1, 0)]
    audio.save()

    await _run(card, tmp_path, lookup=False)

    [track] = await _tracks(db)
    assert track.synced_at is not None and track.quality.startswith("aac_")
    assert abs(os.stat(track.file_path).st_mtime - src.stat().st_mtime) < 2


@pytest.mark.asyncio
async def test_a_second_run_creates_nothing_new(db, card, tmp_path, providers):
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")

    await _run(card, tmp_path)
    again = await _run(card, tmp_path)

    assert len(await _tracks(db)) == 1
    assert again.counts()["imported"] == 0 and again.counts()["skipped_existing"] == 1


@pytest.mark.asyncio
async def test_lookups_are_cached_for_the_next_run(db, card, tmp_path, providers):
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")

    await _run(card, tmp_path, dry_run=True)
    await _run(card, tmp_path, dry_run=True)

    assert [c[0] for c in providers["calls"]] == ["spotify", "youtube"]


@pytest.mark.asyncio
async def test_a_song_the_library_already_has_is_not_imported_again(db, card, tmp_path, providers):
    existing = tmp_path / "existing.m4a"
    existing.write_bytes(b"premium")
    async with db() as session:
        session.add(Track(title="Song", artist="Artist", album="Album", status="done", quality="aac_256",
                          spotify_uri="spotify:track:Song", file_path=str(existing)))
        await session.commit()
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")
    providers["spotify"]["Album"] = [_album("Album", [("Song", 1000, 1, 1)])]

    report = await _run(card, tmp_path)

    assert len(await _tracks(db)) == 1
    assert report.lists["skipped_existing"][0]["library_quality"] == "aac_256"
    assert not (settings.library_dir / "Artist").exists()


@pytest.mark.asyncio
async def test_the_same_release_in_two_card_folders_is_imported_once(db, card, tmp_path, providers):
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")
    card("Artist, Guest/Album/Song.mp3", title="Song", artist="Artist, Guest", album="Album")
    providers["spotify"]["Album"] = [_album("Album", [("Song", 1000, 1, 1)])]

    report = await _run(card, tmp_path)

    assert len(await _tracks(db)) == 1 and report.counts()["skipped_existing"] == 1


@pytest.mark.asyncio
async def test_a_library_file_of_another_track_is_never_overwritten(db, card, tmp_path, providers):
    taken = settings.library_dir / "Artist" / "Album" / "Song.mp3"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"someone else")
    async with db() as session:
        session.add(Track(title="Other title", artist="Other", status="done", file_path=str(taken)))
        await session.commit()
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")

    report = await _run(card, tmp_path, lookup=False)

    assert taken.read_bytes() == b"someone else"
    assert report.counts()["collisions"] == 1 and len(await _tracks(db)) == 1


@pytest.mark.asyncio
async def test_dry_run_writes_no_file_and_no_row_but_still_looks_up(db, card, tmp_path, providers):
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")

    report = await _run(card, tmp_path, dry_run=True)

    assert report.counts()["imported"] == 1 and providers["calls"]
    assert await _tracks(db) == [] and not settings.library_dir.exists()


@pytest.mark.asyncio
async def test_a_low_confidence_album_is_imported_as_it_is(db, card, tmp_path, providers):
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")
    providers["youtube"]["Album"] = [_album("Album", [("Different", 1000, 1, None)], provider="youtube")]

    report = await _run(card, tmp_path)

    [track] = await _tracks(db)
    assert track.track_number is None and track.youtube_id is None
    assert report.lists["low_confidence_albums"][0]["folder"] == "Artist/Album"


@pytest.mark.asyncio
async def test_card_playlists_become_library_playlists(db, card, tmp_path, providers):
    card("Artist/Album/01 - Song.mp3", title="Song", artist="Artist", album="Album", track="1")
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")
    card("Artist/Album/02 - Other.mp3", title="Other", artist="Artist", album="Album", track="2")
    body = "#EXTM3U\r\nartist/album/SONG.mp3\r\nArtist/Album/02 - Other.mp3\r\nGone/Missing.mp3\r\n"
    (card.root / "Mix.m3u8").write_bytes(b"\xef\xbb\xbf" + body.encode())
    (card.root / "Old.m3u").write_text("Artist/Album/Song.mp3")

    report = await _run(card, tmp_path, lookup=False)
    await _run(card, tmp_path, lookup=False)

    async with db() as session:
        [playlist] = (await session.scalars(select(Playlist))).all()
        entries = (await session.scalars(select(PlaylistTrack).order_by(PlaylistTrack.position))).all()
        titles = [(await session.get(Track, e.track_id)).title for e in entries]
    assert (playlist.name, playlist.source, playlist.source_id) == ("Mix", "card", "Mix")
    assert titles == ["Song", "Other"]
    assert report.lists["playlists"][0]["unresolved"] == ["Gone/Missing.mp3"]


@pytest.mark.asyncio
async def test_the_next_sync_rewrites_the_card_playlist_under_its_own_name(client, db, fake_device, tmp_path,
                                                                          originals, monkeypatch, providers):
    from services.playlist import MANIFEST_NAME

    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    song = fake_device / "Artist" / "Album" / "Song.mp3"
    song.parent.mkdir(parents=True)
    shutil.copyfile(originals["mp3"], song)
    _write_tags(song, {"title": "Song", "artist": "Artist", "album": "Album"})
    (fake_device / "Road Trip.m3u8").write_bytes(b"\xef\xbb\xbfArtist/Album/Song.mp3\r\n")
    await card_import.import_card(fake_device, tmp_path / "report.json", lookup=False)

    start = await client.post("/api/device/sync/start", data={"device_path": str(fake_device), "scope": "all"})
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for _ in resp.aiter_lines():
            pass

    assert sorted(p.name for p in fake_device.glob("*.m3u8")) == ["Road Trip.m3u8"]
    assert json.loads((fake_device / MANIFEST_NAME).read_text())["files"] == ["Road Trip.m3u8"]


def test_get_album_tracks_browses_with_the_client_it_is_given(monkeypatch):
    from services import youtube_client

    class Anonymous:
        def get_album(self, browse_id):
            return {"title": "Album", "artists": [{"name": "Artist"}], "tracks": [
                {"videoId": "v1", "title": "Song", "artists": [{"name": "Artist"}], "trackNumber": 3,
                 "duration": "3:20"}]}

    monkeypatch.setattr(youtube_client, "get_client", lambda: None)

    data = youtube_client.get_album_tracks("MPRE1", client=Anonymous())

    assert data["tracks"][0]["track_number"] == 3 and data["tracks"][0]["duration_ms"] == 200_000


def test_a_file_without_a_disc_number_gets_no_inferred_disc_total():
    files = [_cf("A/B/01 - Y.mp3", "Y", track_number=1, disc_number=2), _cf("A/B/07 - X.mp3", "X", track_number=7)]

    infer_total_discs(files)

    assert [f.total_discs for f in files] == [2, None]


def test_an_exact_title_tie_goes_to_the_track_with_the_files_own_number():
    files = [_cf("J/Seven/02 - Seven.mp3", "Seven", 184_440, track_number=2)]
    album = _album("Seven", [("Seven", 184_160, 1, 1), ("Seven (Clean)", 184_400, 2, 1)])
    album["tracks"][1]["title"] = "Seven"
    match = match_album(files, "Seven", album)

    conflicts = apply_match(files, match)

    assert conflicts == [] and files[0].spotify_uri == "spotify:track:Seven (Clean)"


def test_an_unnumbered_file_with_two_equal_candidates_gets_no_number():
    files = [_cf("C/Sinf/Perdoname.mp3", "Perdóname", 247_000), _cf("C/Sinf/03 - Otra.mp3", "Otra", track_number=3)]
    album = _album("Album", [("Perdóname", 247_080, 1, 1), ("Otra", 200_000, 3, 1), ("Perdóname", 246_866, 12, 1)])

    match = match_album(files, "Album", album)
    apply_match(files, match)

    assert files[0].track_number is None and files[0].spotify_uri is None and match.ambiguous == [files[0]]


def test_a_match_that_contradicts_the_cards_number_applies_no_ids_or_totals():
    files = [_cf("T/The Feels/01 - The Feels.mp3", "The Feels", track_number=1)]
    tracks = [("The Feels (Remix)", 200_000, 1, 1), ("The Feels", 200_000, 2, 1)] + [
        (f"Mix {n}", 100_000, n, 1) for n in range(3, 10)]
    match = match_album(files, "The Feels", _album("The Feels", tracks))

    conflicts = apply_match(files, match)

    assert [c["lookup_value"] for c in conflicts] == [2]
    assert (files[0].spotify_uri, files[0].total_tracks) == (None, None)


def test_a_release_without_disc_numbers_numbers_no_file_past_disc_one():
    files = [_cf("M/Dos/Uno.mp3", "Uno", 200_000, disc_number=1, total_discs=2),
             _cf("M/Dos/Cinco.mp3", "Cinco", 210_000, disc_number=2, total_discs=2)]
    match = match_album(files, "Album", _album("Album", [("Uno", 200_000, 1, None), ("Cinco", 210_000, 14, None)],
                                               provider="youtube"))

    conflicts = apply_match(files, match)

    assert [f.track_number for f in files] == [1, None]
    assert [(c["card"], c["lookup_value"]) for c in conflicts] == [("M/Dos/Cinco.mp3", 14)]


def test_two_recordings_with_one_title_in_an_album_are_both_imported(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    index = card_import.LibraryIndex([])
    first = _cf("A/Alb/1-01 - Intro.mp3", "Intro", 60_000, track_number=1, disc_number=1, total_discs=2)
    second = _cf("A/Alb/2-01 - Intro.mp3", "Intro", 95_000, track_number=1, disc_number=2, total_discs=2)

    plans = [card_import.plan_file(f, index) for f in (first, second)]

    assert [p.outcome for p in plans] == ["import", "import"]


@pytest.mark.asyncio
async def test_a_failed_copy_leaves_its_other_card_copy_unresolved_and_the_report_written(db, card, tmp_path, providers,
                                                                                         monkeypatch):
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")
    card("Artist, Guest/Album/Song.mp3", title="Song", artist="Artist, Guest", album="Album")
    (card.root / "Mix.m3u8").write_bytes(b"Artist, Guest/Album/Song.mp3\r\nArtist/Album/Song.mp3\r\n")
    providers["spotify"]["Album"] = [_album("Album", [("Song", 1000, 1, 1)])]

    def eio(plan):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(card_import, "install_file", eio)

    report = await _run(card, tmp_path)

    async with db() as session:
        assert (await session.scalars(select(PlaylistTrack))).all() == []
    assert (tmp_path / "report" / "report.json").exists()
    assert report.counts()["errors"] == 2 and report.lists["stale_card_copies"] == []


@pytest.mark.asyncio
async def test_a_trial_run_with_an_album_limit_imports_no_playlist(db, card, tmp_path, providers):
    card("A/Alb1/S1.mp3", title="S1", artist="A", album="Alb1")
    card("B/Alb2/S2.mp3", title="S2", artist="B", album="Alb2")
    (card.root / "Mix.m3u8").write_bytes(b"A/Alb1/S1.mp3\r\nB/Alb2/S2.mp3\r\n")

    report = await _run(card, tmp_path, lookup=False, limit_albums=1)

    async with db() as session:
        assert (await session.scalars(select(Playlist))).all() == []
    assert report.playlists_skipped


@pytest.mark.asyncio
async def test_card_playlists_are_backed_up_byte_for_byte_before_they_are_stored(db, card, tmp_path, providers):
    card("A/Alb1/S1.mp3", title="S1", artist="A", album="Alb1")
    original = b"\xef\xbb\xbf#EXTM3U\r\nA/Alb1/S1.mp3\r\nGone/Missing.mp3\r\n"
    (card.root / "Mix.m3u8").write_bytes(original)

    await _run(card, tmp_path, lookup=False)

    [backup] = (tmp_path / "report" / "card-playlists").iterdir()
    assert backup.read_bytes() == original


@pytest.mark.asyncio
async def test_a_rerun_fills_the_ids_and_numbers_a_throttled_first_run_missed(db, card, tmp_path, providers, monkeypatch):
    from mutagen.id3 import ID3

    answering = card_import.PROVIDERS["spotify"]

    def throttled(artist, album):
        raise card_import.ProviderThrottled("spotify asked to wait 83322 s")

    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")
    monkeypatch.setitem(card_import.PROVIDERS, "spotify", throttled)
    await _run(card, tmp_path)
    monkeypatch.setitem(card_import.PROVIDERS, "spotify", answering)
    providers["spotify"]["Album"] = [_album("Album", [("Song", 1000, 4, 1)])]

    report = await _run(card, tmp_path)

    [track] = await _tracks(db)
    dest = settings.library_dir / "Artist" / "Album" / "04 - Song.mp3"
    assert (track.spotify_uri, track.track_number, track.file_path, track.synced_at) == (
        "spotify:track:Song", 4, str(dest), None)
    assert str(ID3(dest)["TRCK"]) == "4/1" and report.counts()["enriched"] == 1


@pytest.mark.asyncio
async def test_a_card_copy_of_a_queued_song_is_neither_stale_nor_already_in_the_library(db, card, tmp_path, providers):
    async with db() as session:
        session.add(Track(title="Song", artist="Artist", album="Album", status="pending", format="mp3"))
        await session.commit()
    card("Artist/Album/Song.m4a", title="Song", artist="Artist", album="Album")

    report = await _run(card, tmp_path, lookup=False)

    assert report.lists["stale_card_copies"] == [] and report.counts()["skipped_existing"] == 0
    assert report.counts()["queued"] == 1


@pytest.mark.asyncio
async def test_a_retag_that_fails_is_an_error_not_a_retagged_file(db, card, tmp_path, providers, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("malformed atom")

    monkeypatch.setattr(card_import.tagger, "_tag_mp3", broken)
    card("Artist/Album/Song.mp3", title="Song", artist="Artist", album="Album")

    report = await _run(card, tmp_path, lookup=False)

    assert report.counts()["errors"] == 1 and report.counts()["retagged"] == 0
    assert await _tracks(db) == [] and not (settings.library_dir / "Artist" / "Album" / "Song.mp3").exists()


async def _sync(client, device: Path) -> None:
    start = await client.post("/api/device/sync/start", data={"device_path": str(device), "scope": "all"})
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for _ in resp.aiter_lines():
            pass


@pytest.fixture
def renumbered_card(fake_device, tmp_path, originals, monkeypatch, providers):
    """The H2 holding 'Song.mp3' (+ .jpg/.lrc) that the lookup numbers 04; returns (old path, report path)."""
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    song = fake_device / "Artist" / "Album" / "Song.mp3"
    song.parent.mkdir(parents=True)
    shutil.copyfile(originals["mp3"], song)
    _write_tags(song, {"title": "Song", "artist": "Artist", "album": "Album", "album_artist": "Artist"})
    for suffix in (".jpg", ".lrc"):
        song.with_suffix(suffix).write_bytes(suffix.encode())
    providers["spotify"]["Album"] = [_album("Album", [("Song", 1000, 4, 1)])]
    return song, tmp_path / "report" / "report.json"


@pytest.mark.asyncio
async def test_cleaning_after_a_sync_removes_the_stale_copy_and_its_sidecars(client, db, fake_device, renumbered_card):
    song, report = renumbered_card
    await card_import.import_card(fake_device, report)
    await _sync(client, fake_device)

    result = await card_import.clean_stale_card_copies(str(fake_device), report, apply=True)

    assert [e["card"] for e in result["removed"]] == ["Artist/Album/Song.mp3"]
    assert sorted(p.name for p in song.parent.iterdir()) == ["04 - Song.jpg", "04 - Song.lrc", "04 - Song.mp3"]


@pytest.mark.asyncio
async def test_cleaning_without_apply_deletes_nothing(client, db, fake_device, renumbered_card):
    song, report = renumbered_card
    await card_import.import_card(fake_device, report)
    await _sync(client, fake_device)

    result = await card_import.clean_stale_card_copies(str(fake_device), report)

    assert len(result["removed"]) == 1 and song.exists() and song.with_suffix(".lrc").exists()


@pytest.mark.asyncio
async def test_cleaning_before_the_sync_keeps_the_only_copy(db, fake_device, renumbered_card):
    song, report = renumbered_card
    await card_import.import_card(fake_device, report)

    result = await card_import.clean_stale_card_copies(str(fake_device), report, apply=True)

    assert result["removed"] == [] and song.exists()
    assert result["kept"][0]["reason"] == "the replacement is not on the card yet; sync first"


@pytest.mark.asyncio
async def test_cleaning_refuses_a_path_that_is_not_a_detected_drive(db, fake_device, tmp_path):
    report = tmp_path / "report.json"
    report.write_text('{"stale_card_copies": []}')

    with pytest.raises(ValueError):
        await card_import.clean_stale_card_copies(str(tmp_path), report, apply=True)


def _done_row(path: Path, **fields) -> Track:
    return Track(title="Song", artist="Artist", album="Album", album_artist="Artist", status="done",
                 file_path=str(path), **fields)


def test_a_sidecar_shared_with_the_kept_copy_of_a_duplicate_is_not_removed(tmp_path):
    card, library = tmp_path / "card", tmp_path / "library"
    (card / "Artist" / "Album").mkdir(parents=True)
    library.mkdir()
    for name in ("Song.mp3", "Song.m4a", "Song.jpg"):
        (card / "Artist" / "Album" / name).write_bytes(b"x" * 10)
    (library / "Song.m4a").write_bytes(b"x" * 10)
    entry = {"card": "Artist/Album/Song.mp3", "replaced_by": "Artist/Album/Song.m4a", "reason": "duplicate"}

    result = card_import.remove_stale_copies(card, [entry], [_done_row(library / "Song.m4a")], apply=True)

    assert result["removed"][0]["sidecars"] == []
    assert sorted(p.name for p in (card / "Artist" / "Album").iterdir()) == ["Song.jpg", "Song.m4a"]


def test_a_report_entry_outside_the_card_is_never_removed(tmp_path):
    card, outside = tmp_path / "card", tmp_path / "victim.mp3"
    card.mkdir()
    outside.write_bytes(b"x")

    result = card_import.remove_stale_copies(card, [{"card": "../victim.mp3", "replaced_by": "A/B/01 - Song.mp3"}],
                                             [], apply=True)

    assert result["kept"][0]["reason"] == "not an audio path on the card" and outside.exists()
