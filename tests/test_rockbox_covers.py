"""Rockbox shows cover.jpg from the album folder and cannot decode progressive JPEG. Leasure writes a
baseline cover.jpg (<= 500 px) per album on Rockbox cards, records its own in .leasure-covers.json,
and only ever replaces or removes a cover whose bytes are still the ones it wrote."""

import io
import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import delete

from models import Track
from services import artwork, card_import, covers
from services.covers import COVERS_MANIFEST
from tests.rockbox_fakes import add_rockbox, jpeg, seed_track, sync, tree_digest

COVER = ("Artist", "Album", "cover.jpg")


def _image(path) -> Image.Image:
    img = Image.open(io.BytesIO(path.read_bytes()))
    img.load()
    return img


def _records(card) -> dict:
    manifest = card / COVERS_MANIFEST
    return json.loads(manifest.read_text(encoding="utf-8"))["covers"] if manifest.exists() else {}


@pytest.fixture
def rockbox_card(fake_device):
    add_rockbox(fake_device)
    return fake_device


@pytest.mark.asyncio
async def test_each_album_gets_a_baseline_cover_of_at_most_500_px(client, db, rockbox_card, tmp_path):
    await seed_track(db, tmp_path, art=jpeg(1200, progressive=True))
    await seed_track(db, tmp_path, title="Other", number=2, art=jpeg(1200, color=(0, 0, 255)))

    done = await sync(client, rockbox_card)

    cover = rockbox_card.joinpath(*COVER)
    img = _image(cover)
    assert img.format == "JPEG" and max(img.size) <= 500
    assert not img.info.get("progressive") and not img.info.get("progression")
    assert img.getpixel((10, 10))[0] > 150, "the first track's art, in album order"
    assert list(_records(rockbox_card)) == ["Artist/Album/cover.jpg"]
    assert done["covers_written"] == 1


@pytest.mark.parametrize("name", ["cover.jpg", "Cover.JPG"])
@pytest.mark.asyncio
async def test_a_cover_the_user_put_there_is_left_alone(client, db, rockbox_card, tmp_path, name):
    await seed_track(db, tmp_path, art=jpeg(600))
    folder = rockbox_card / "Artist" / "Album"
    folder.mkdir(parents=True)
    (folder / name).write_bytes(b"user art")

    done = await sync(client, rockbox_card)

    assert (folder / name).read_bytes() == b"user art"
    assert sorted(p.name for p in folder.iterdir() if p.name.casefold() == "cover.jpg") == [name]
    assert _records(rockbox_card) == {} and done["covers_written"] == 0


@pytest.mark.asyncio
async def test_leasures_cover_is_replaced_when_the_album_art_changes(client, db, rockbox_card, tmp_path):
    track = await seed_track(db, tmp_path, art=jpeg(600, color=(255, 0, 0)))
    await sync(client, rockbox_card)
    sidecar = Path(track.file_path).with_suffix(".jpg")
    sidecar.write_bytes(jpeg(600, color=(0, 255, 0)))
    os.utime(sidecar, (1_800_000_000, 1_800_000_000))

    done = await sync(client, rockbox_card)

    assert _image(rockbox_card.joinpath(*COVER)).getpixel((10, 10))[1] > 200
    assert done["covers_written"] == 1


@pytest.mark.asyncio
async def test_a_cover_edited_on_the_card_is_kept_and_no_longer_ours(client, db, rockbox_card, tmp_path):
    track = await seed_track(db, tmp_path, art=jpeg(600, color=(255, 0, 0)))
    await sync(client, rockbox_card)
    cover = rockbox_card.joinpath(*COVER)
    cover.write_bytes(b"edited by hand")
    sidecar = Path(track.file_path).with_suffix(".jpg")
    sidecar.write_bytes(jpeg(600, color=(0, 255, 0)))

    await sync(client, rockbox_card)

    assert cover.read_bytes() == b"edited by hand" and _records(rockbox_card) == {}


@pytest.mark.asyncio
async def test_a_second_sync_rewrites_nothing(client, db, rockbox_card, tmp_path):
    await seed_track(db, tmp_path, art=jpeg(600))
    await sync(client, rockbox_card)
    cover = rockbox_card.joinpath(*COVER)
    before = cover.stat().st_mtime_ns

    done = await sync(client, rockbox_card)

    assert cover.stat().st_mtime_ns == before and done["covers_written"] == 0


async def _drop_tracks(db) -> None:
    async with db() as session:
        await session.execute(delete(Track))
        await session.commit()


@pytest.mark.asyncio
async def test_a_cover_of_an_album_that_left_the_library_is_removed_with_its_empty_folders(client, db, rockbox_card,
                                                                                         tmp_path):
    await seed_track(db, tmp_path, art=jpeg(600))
    await sync(client, rockbox_card)
    for f in (rockbox_card / "Artist" / "Album").iterdir():
        if f.name != "cover.jpg":
            f.unlink()
    await _drop_tracks(db)
    await seed_track(db, tmp_path, artist="Else", album="Other", art=None)
    before = tree_digest(rockbox_card / ".rockbox")

    done = await sync(client, rockbox_card)

    assert done["covers_removed"] == 1
    assert not (rockbox_card / "Artist").exists()
    assert _records(rockbox_card) == {}
    assert tree_digest(rockbox_card / ".rockbox") == before


@pytest.mark.asyncio
async def test_a_user_cover_in_a_gone_album_is_kept(client, db, rockbox_card, tmp_path):
    folder = rockbox_card / "Gone" / "Album"
    folder.mkdir(parents=True)
    (folder / "cover.jpg").write_bytes(b"user art")
    await seed_track(db, tmp_path, art=None)

    await sync(client, rockbox_card)

    assert (folder / "cover.jpg").read_bytes() == b"user art"


@pytest.mark.asyncio
async def test_a_new_only_sync_keeps_covers_of_albums_still_in_the_library(client, db, rockbox_card, tmp_path):
    await seed_track(db, tmp_path, art=jpeg(600))
    await sync(client, rockbox_card)

    done = await sync(client, rockbox_card, scope="new")

    assert rockbox_card.joinpath(*COVER).exists() and done["covers_removed"] == 0


@pytest.mark.asyncio
async def test_a_corrupt_covers_manifest_means_nothing_is_ours(client, db, rockbox_card, tmp_path):
    await seed_track(db, tmp_path, art=jpeg(600))
    await sync(client, rockbox_card)
    cover = rockbox_card.joinpath(*COVER)
    data = cover.read_bytes()
    (rockbox_card / COVERS_MANIFEST).write_text("{not json")
    await _drop_tracks(db)
    await seed_track(db, tmp_path, artist="Else", album="Other", art=jpeg(600, color=(0, 0, 255)))

    done = await sync(client, rockbox_card)

    assert cover.read_bytes() == data and done["covers_removed"] == 0


@pytest.mark.asyncio
async def test_a_card_that_left_rockbox_still_loses_leasures_orphaned_covers(client, db, rockbox_card, tmp_path):
    await seed_track(db, tmp_path, art=jpeg(600))
    await sync(client, rockbox_card)
    shutil.rmtree(rockbox_card / ".rockbox")
    await _drop_tracks(db)
    await seed_track(db, tmp_path, artist="Else", album="Other", art=jpeg(600))

    done = await sync(client, rockbox_card)

    assert done["covers_removed"] == 1 and done["covers_written"] == 0
    assert not list(rockbox_card.rglob("cover.jpg"))


def test_covers_are_never_written_inside_a_protected_folder(tmp_path):
    add_rockbox(tmp_path)
    (tmp_path / ".rockbox" / "Album").mkdir()
    sidecar = tmp_path / "art.jpg"
    sidecar.write_bytes(jpeg(600))
    before = tree_digest(tmp_path / ".rockbox")

    result = covers.write_album_covers(tmp_path, {".rockbox/Album": sidecar, "../Album": sidecar})

    assert result.written == 0 and tree_digest(tmp_path / ".rockbox") == before


def test_prepare_cover_turns_progressive_art_into_baseline():
    out = artwork.prepare_cover(jpeg(800, progressive=True), 500)
    img = Image.open(io.BytesIO(out))
    assert not img.info.get("progressive") and not img.info.get("progression") and img.size == (500, 500)


@pytest.mark.parametrize("data, baseline", [(jpeg(50), True), (jpeg(50, progressive=True), False), (b"not art", False)])
def test_is_baseline_jpeg(data, baseline):
    assert artwork.is_baseline_jpeg(data) is baseline


def test_importing_a_progressive_card_sidecar_stores_a_baseline_one(tmp_path):
    card = tmp_path / "card" / "Artist" / "Album"
    card.mkdir(parents=True)
    song = card / "01 - Song.mp3"
    song.write_bytes(b"\xff\xfb" * 50)
    song.with_suffix(".jpg").write_bytes(jpeg(800, progressive=True))
    before = tree_digest(tmp_path / "card")
    dest = tmp_path / "library" / "01 - Song.mp3"
    f = card_import.CardFile(rel="Artist/Album/01 - Song.mp3", path=song, size=100, mtime=0.0, title="Song",
                             artist="Artist", album="Album", album_artist=None)
    plan = card_import.Planned(f, Track(title="Song"), dest, "Artist/Album/01 - Song.mp3", retag=False,
                               outcome="import")

    card_import.install_file(plan)

    assert artwork.is_baseline_jpeg(dest.with_suffix(".jpg").read_bytes())
    assert tree_digest(tmp_path / "card") == before


async def _unnumbered_cover_track(db, tmp_path) -> Track:
    src = tmp_path / "lib" / "Cover.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\xff\xfb\x90\x00" * 50)
    src.with_suffix(".jpg").write_bytes(jpeg(300))
    async with db() as session:
        track = Track(title="Cover", artist="Artist", album="Album", status="done", file_path=str(src), format="mp3")
        session.add(track)
        await session.commit()
    return track


@pytest.mark.asyncio
async def test_a_track_titled_cover_never_writes_a_cover_jpg_sidecar_on_a_rockbox_card(client, db, rockbox_card,
                                                                                       tmp_path):
    await _unnumbered_cover_track(db, tmp_path)
    folder = rockbox_card / "Artist" / "Album"
    folder.mkdir(parents=True)
    (folder / "cover.jpg").write_bytes(b"user art")

    done = await sync(client, rockbox_card)

    assert (folder / "Cover.mp3").is_file() and done["copied"] == 1
    assert sorted(p.name for p in folder.iterdir() if p.name.casefold() == "cover.jpg") == ["cover.jpg"]
    assert (folder / "cover.jpg").read_bytes() == b"user art"


@pytest.mark.asyncio
async def test_a_track_titled_cover_keeps_its_sidecar_on_a_stock_card(client, db, fake_device, tmp_path):
    await _unnumbered_cover_track(db, tmp_path)

    await sync(client, fake_device)

    assert (fake_device / "Artist" / "Album" / "Cover.jpg").is_file()


@pytest.mark.asyncio
async def test_a_progressive_library_sidecar_reaches_a_rockbox_card_as_baseline(client, db, rockbox_card, tmp_path):
    art = jpeg(600, progressive=True)
    track = await seed_track(db, tmp_path, art=art)

    await sync(client, rockbox_card)
    again = await sync(client, rockbox_card)

    assert artwork.is_baseline_jpeg(rockbox_card.joinpath("Artist", "Album", "01 - Song.jpg").read_bytes())
    assert Path(track.file_path).with_suffix(".jpg").read_bytes() == art
    assert (again["copied"], again["skipped"]) == (0, 1)


@pytest.mark.asyncio
async def test_a_stock_sync_leaves_a_progressive_library_sidecar_as_it_is(client, db, fake_device, tmp_path):
    art = jpeg(600, progressive=True)
    track = await seed_track(db, tmp_path, art=art)

    await sync(client, fake_device)

    assert Path(track.file_path).with_suffix(".jpg").read_bytes() == art


def test_rockbox_sidecar_is_the_file_itself_for_a_small_baseline_jpeg(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(jpeg(300))
    before = path.stat().st_mtime_ns

    assert artwork.rockbox_sidecar(path, 500) == path
    assert path.stat().st_mtime_ns == before
