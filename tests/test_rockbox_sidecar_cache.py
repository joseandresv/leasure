"""A sync never writes the library. On a Rockbox card a sidecar Rockbox cannot show (progressive, or over
500 px) reaches the card as a baseline copy cached under data/card-cache/covers; stock cards get it verbatim."""

import hashlib
import io
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from config import settings
from services import artwork
from tests.rockbox_fakes import add_rockbox, jpeg, seed_track, sync

CARD_ART = ("Artist", "Album", "01 - Song.jpg")
OLD_MTIME_NS = 1_600_000_000 * 10**9


@pytest.fixture
def rockbox_card(fake_device):
    add_rockbox(fake_device)
    return fake_device


async def _track_with_art(db, tmp_path, art: bytes) -> Path:
    track = await seed_track(db, tmp_path, art=art)
    sidecar = Path(track.file_path).with_suffix(".jpg")
    os.utime(sidecar, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    return sidecar


def _cached(art: bytes) -> Path:
    size = min(settings.artwork_size, 500)
    return settings.data_dir / "card-cache" / "covers" / f"{hashlib.sha256(art).hexdigest()}-{size}.jpg"


@pytest.mark.asyncio
async def test_a_rockbox_sync_leaves_a_progressive_library_sidecar_untouched(client, db, rockbox_card, tmp_path):
    art = jpeg(600, progressive=True, color=(10, 200, 10))
    sidecar = await _track_with_art(db, tmp_path, art)

    await sync(client, rockbox_card)

    assert sidecar.read_bytes() == art and sidecar.stat().st_mtime_ns == OLD_MTIME_NS


@pytest.mark.asyncio
async def test_the_rockbox_card_gets_the_cached_baseline_copy(client, db, rockbox_card, tmp_path):
    art = jpeg(600, progressive=True, color=(10, 20, 200))
    await _track_with_art(db, tmp_path, art)

    await sync(client, rockbox_card)

    card_art = rockbox_card.joinpath(*CARD_ART).read_bytes()
    assert artwork.is_baseline_jpeg(card_art) and card_art == _cached(art).read_bytes()


@pytest.mark.asyncio
async def test_an_oversized_baseline_sidecar_reaches_a_rockbox_card_at_500_px(client, db, rockbox_card, tmp_path):
    art = jpeg(800, color=(120, 120, 10))
    sidecar = await _track_with_art(db, tmp_path, art)

    await sync(client, rockbox_card)

    assert Image.open(io.BytesIO(rockbox_card.joinpath(*CARD_ART).read_bytes())).size == (500, 500)
    assert sidecar.read_bytes() == art


@pytest.mark.asyncio
async def test_a_second_rockbox_sync_copies_nothing(client, db, rockbox_card, tmp_path, monkeypatch):
    await _track_with_art(db, tmp_path, jpeg(600, progressive=True, color=(200, 10, 200)))
    await sync(client, rockbox_card)
    card_art = rockbox_card.joinpath(*CARD_ART)
    before = card_art.stat().st_mtime_ns
    copied: list[str] = []
    real_copyfile = shutil.copyfile
    monkeypatch.setattr(shutil, "copyfile", lambda src, dst, **kw: copied.append(str(dst)) or real_copyfile(src, dst, **kw))

    again = await sync(client, rockbox_card)

    assert (again["copied"], again["skipped"], copied) == (0, 1, [])
    assert card_art.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_a_synced_rockbox_card_needs_no_space_for_its_cached_art(client, db, rockbox_card, tmp_path):
    from routers import device

    track = await seed_track(db, tmp_path, art=jpeg(600, progressive=True, color=(90, 200, 160)))
    await sync(client, rockbox_card)

    plan = device._plan_space(rockbox_card, [track], "rockbox")

    assert plan.needed_bytes == 0


@pytest.mark.asyncio
async def test_a_stock_card_gets_a_progressive_sidecar_verbatim(client, db, fake_device, tmp_path):
    art = jpeg(600, progressive=True, color=(250, 140, 0))
    sidecar = await _track_with_art(db, tmp_path, art)

    await sync(client, fake_device)

    assert fake_device.joinpath(*CARD_ART).read_bytes() == art
    assert sidecar.read_bytes() == art and sidecar.stat().st_mtime_ns == OLD_MTIME_NS
    assert not _cached(art).exists()


@pytest.mark.asyncio
async def test_a_rockbox_sync_does_not_make_the_stock_card_copy_the_sidecar_again(client, db, fake_device, tmp_path,
                                                                                monkeypatch):
    import services.device as device_mod

    rockbox = tmp_path / "Rockbox"
    rockbox.mkdir()
    add_rockbox(rockbox)
    volumes = device_mod.detect_devices() + [{**device_mod.detect_devices()[0], "path": str(rockbox)}]
    monkeypatch.setattr(device_mod, "detect_devices", lambda: list(volumes))
    await _track_with_art(db, tmp_path, jpeg(600, progressive=True, color=(30, 160, 230)))
    await sync(client, fake_device)
    await sync(client, rockbox)
    copied: list[str] = []
    real_copyfile = shutil.copyfile
    monkeypatch.setattr(shutil, "copyfile", lambda src, dst, **kw: copied.append(str(dst)) or real_copyfile(src, dst, **kw))

    await sync(client, fake_device)

    assert copied == []
