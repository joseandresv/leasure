import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

import services.device as device_mod
from models import CardCopy, Playlist, PlaylistTrack, SupersededFile, Track

OLD_REL = "Artist/Album/01 - Song.m4a"
NEW_REL = "Artist/Album/01 - Song.flac"


async def _seed(db, tmp_path) -> tuple[Track, Path]:
    folder = tmp_path / "lib" / "Artist" / "Album"
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "01 - Song.m4a"
    src.write_bytes(b"old-audio" * 100)
    src.with_suffix(".jpg").write_bytes(b"jpg")
    src.with_suffix(".lrc").write_bytes(b"[00:01.00]la")
    async with db() as session:
        track = Track(title="Song", artist="Artist", album="Album", track_number=1, status="done",
                      file_path=str(src), format="m4a")
        session.add(track)
        await session.commit()
    return track, src


def _head(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()[:1024 * 1024]).hexdigest()


async def _upgrade_by_hand(db, track: Track, old: Path, *, bases=("card_copy", "library"), write_flac=True) -> Path:
    """What an upgrade leaves behind: the row on a FLAC, the m4a gone from the library, SupersededFile rows."""
    flac = old.with_suffix(".flac")
    if write_flac:
        flac.write_bytes(b"fLaC" + b"\x01" * 2000)
    st = old.stat()
    async with db() as session:
        row = await session.get(Track, track.id)
        row.file_path, row.format, row.synced_at = str(flac), "flac", None
        if "card_copy" in bases:
            copies = await session.scalars(select(CardCopy).where(CardCopy.track_id == track.id, CardCopy.kind == "audio"))
            for c in copies.all():
                session.add(SupersededFile(track_id=track.id, card_id=c.card_id, basis="card_copy", kind="audio",
                                           rel_path=c.rel_path, size=c.size, mtime=c.mtime, head_sha256=c.head_sha256))
        if "library" in bases:
            session.add(SupersededFile(track_id=track.id, card_id=None, basis="library", kind="audio", rel_path=OLD_REL,
                                       size=st.st_size, mtime=st.st_mtime, head_sha256=_head(old)))
        await session.commit()
    old.unlink()
    return flac


async def _sync(client, card: Path, scope: str = "all") -> dict:
    start = await client.post("/api/device/sync/start", data={"device_path": str(card), "scope": scope})
    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events[-1]


async def _synced_then_upgraded(client, db, card, tmp_path, **upgrade) -> Track:
    track, src = await _seed(db, tmp_path)
    await _sync(client, card)
    await _upgrade_by_hand(db, track, src, **upgrade)
    return track


@pytest.mark.asyncio
async def test_the_old_copy_goes_once_the_flac_is_on_the_card(client, db, fake_device, tmp_path):
    track = await _synced_then_upgraded(client, db, fake_device, tmp_path)

    done = await _sync(client, fake_device)

    assert (done["removed"], done["removed_paths"], done["kept"]) == (1, [OLD_REL], 0)
    assert not (fake_device / OLD_REL).exists() and (fake_device / NEW_REL).exists()
    assert (fake_device / "Artist/Album/01 - Song.jpg").exists() and (fake_device / "Artist/Album/01 - Song.lrc").exists()
    async with db() as session:
        records = (await session.scalars(select(SupersededFile))).all()
        copies = (await session.scalars(select(CardCopy.rel_path).where(CardCopy.track_id == track.id))).all()
    assert all(r.last_removed_at is not None for r in records)
    assert OLD_REL not in copies


@pytest.mark.asyncio
async def test_a_new_only_sync_also_removes_the_old_copy(client, db, fake_device, tmp_path):
    await _synced_then_upgraded(client, db, fake_device, tmp_path)

    done = await _sync(client, fake_device, scope="new")

    assert done["removed"] == 1 and not (fake_device / OLD_REL).exists()


@pytest.fixture
def two_cards(tmp_path, monkeypatch):
    cards = [tmp_path / "H2-A", tmp_path / "H2-B"]
    fake = []
    for card in cards:
        card.mkdir()
        fake.append({"path": str(card), "drive_letter": "", "label": card.name, "total_gb": 32.0, "free_gb": 30.0,
                     "used_gb": 2.0, "has_music_files": True, "device_type": "player", "file_count": 0})
    monkeypatch.setattr(device_mod, "detect_devices", lambda: list(fake))
    return cards


@pytest.mark.asyncio
async def test_a_second_card_loses_its_old_copy_by_the_library_record(client, db, two_cards, tmp_path):
    card_a, card_b = two_cards
    track, src = await _seed(db, tmp_path)
    await _sync(client, card_a)
    await _sync(client, card_b)
    await _upgrade_by_hand(db, track, src)

    done = await _sync(client, card_b)

    assert done["removed"] == 1 and not (card_b / OLD_REL).exists()
    assert (card_a / OLD_REL).exists()
    assert json.loads((card_a / ".leasure-card.json").read_text())["card_id"] != \
        json.loads((card_b / ".leasure-card.json").read_text())["card_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["size", "head"])
async def test_a_copy_changed_on_the_card_is_kept_and_listed(client, db, fake_device, tmp_path, tamper):
    await _synced_then_upgraded(client, db, fake_device, tmp_path)
    old = fake_device / OLD_REL
    st = old.stat()
    old.write_bytes(old.read_bytes() + b"x" if tamper == "size" else b"X" + old.read_bytes()[1:])
    os.utime(old, ns=(st.st_atime_ns, st.st_mtime_ns))

    done = await _sync(client, fake_device)

    assert (done["removed"], done["kept_paths"]) == (0, [{"path": OLD_REL, "reason": "changed_on_card"}])
    assert old.exists()


@pytest.mark.asyncio
async def test_the_old_copy_waits_while_the_flac_cannot_be_copied(client, db, fake_device, tmp_path):
    await _synced_then_upgraded(client, db, fake_device, tmp_path, write_flac=False)

    done = await _sync(client, fake_device)

    assert done["kept_paths"] == [{"path": OLD_REL, "reason": "waiting"}]
    assert (fake_device / OLD_REL).exists()


@pytest.mark.asyncio
async def test_a_path_another_row_maps_to_is_kept(client, db, fake_device, tmp_path):
    await _synced_then_upgraded(client, db, fake_device, tmp_path)
    async with db() as session:
        session.add(Track(title="Song", artist="Artist", album="Album", track_number=1, status="error",
                          file_path=str(tmp_path / "elsewhere" / "Song.m4a"), format="m4a"))
        await session.commit()

    done = await _sync(client, fake_device)

    assert done["kept_paths"] == [{"path": OLD_REL, "reason": "path_in_use"}]
    assert (fake_device / OLD_REL).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", ["../x.m4a", "ABSOLUTE", "Artist/x.m4a", "Artist/../Album/x.m4a"])
async def test_unsafe_record_paths_are_never_deleted(client, db, fake_device, tmp_path, rel):
    track = await _synced_then_upgraded(client, db, fake_device, tmp_path, bases=())
    victim = (tmp_path / "abs.m4a") if rel == "ABSOLUTE" else (fake_device / rel)
    rel = victim.as_posix() if rel == "ABSOLUTE" else rel
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"precious")
    async with db() as session:
        session.add(SupersededFile(track_id=track.id, card_id=None, basis="library", kind="audio", rel_path=rel,
                                   size=8, mtime=victim.stat().st_mtime, head_sha256=_head(victim)))
        await session.commit()

    done = await _sync(client, fake_device)

    assert victim.read_bytes() == b"precious"
    assert (done["removed"], done["errors"]) == (0, [])


@pytest.mark.asyncio
async def test_a_symlinked_album_folder_is_not_followed(client, db, fake_device, tmp_path):
    await _synced_then_upgraded(client, db, fake_device, tmp_path)
    album = fake_device / "Artist" / "Album"
    outside = tmp_path / "outside-album"
    album.rename(outside)
    album.symlink_to(outside, target_is_directory=True)

    done = await _sync(client, fake_device)

    assert done["removed"] == 0 and (outside / "01 - Song.m4a").exists()


@pytest.mark.asyncio
async def test_a_symlinked_file_is_not_deleted(client, db, fake_device, tmp_path):
    await _synced_then_upgraded(client, db, fake_device, tmp_path)
    old = fake_device / OLD_REL
    real = tmp_path / "real.m4a"
    old.rename(real)
    old.symlink_to(real)

    done = await _sync(client, fake_device)

    assert done["removed"] == 0 and old.is_symlink() and real.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("offset", "removed"), [(3599, 1), (3601, 1), (-3601, 1), (1800, 0)])
async def test_an_hour_of_dst_drift_still_counts_as_the_same_copy(client, db, fake_device, tmp_path, offset, removed):
    await _synced_then_upgraded(client, db, fake_device, tmp_path, bases=("card_copy",))
    old = fake_device / OLD_REL
    st = old.stat()
    os.utime(old, (st.st_atime, st.st_mtime + offset))

    done = await _sync(client, fake_device)

    assert done["removed"] == removed


@pytest.mark.asyncio
async def test_emptied_folders_go_but_never_the_card_root(client, db, fake_device, tmp_path):
    track = await _synced_then_upgraded(client, db, fake_device, tmp_path, bases=())
    stray = fake_device / "Gone" / "Album" / "01 - Song.m4a"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"stray-old")
    async with db() as session:
        session.add(SupersededFile(track_id=track.id, card_id=None, basis="library", kind="audio",
                                   rel_path="Gone/Album/01 - Song.m4a", size=9, mtime=stray.stat().st_mtime,
                                   head_sha256=_head(stray)))
        await session.commit()

    done = await _sync(client, fake_device)

    assert done["removed"] == 1
    assert not (fake_device / "Gone").exists()
    assert fake_device.is_dir() and (fake_device / "Artist" / "Album").is_dir()


async def _playlist_with(db, track: Track) -> None:
    async with db() as session:
        playlist = Playlist(name="Mix", source="manual")
        session.add(playlist)
        await session.flush()
        session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=0))
        await session.commit()


def _playlist_lines(card: Path) -> list[str]:
    return (card / "Mix.m3u8").read_bytes().decode("utf-8-sig").split("\r\n")[:-1]


@pytest.mark.asyncio
async def test_the_playlist_lists_the_flac_once_it_is_on_the_card(client, db, fake_device, tmp_path):
    track = await _synced_then_upgraded(client, db, fake_device, tmp_path)
    await _playlist_with(db, track)

    await _sync(client, fake_device)

    assert _playlist_lines(fake_device) == [NEW_REL]


@pytest.mark.asyncio
async def test_the_playlist_keeps_the_old_copy_while_the_flac_is_missing(client, db, fake_device, tmp_path):
    track = await _synced_then_upgraded(client, db, fake_device, tmp_path, write_flac=False)
    await _playlist_with(db, track)

    await _sync(client, fake_device)

    assert _playlist_lines(fake_device) == [OLD_REL]
