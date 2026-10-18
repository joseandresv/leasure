import hashlib
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from models import CardCopy, Track
from routers import device as device_router


async def _seed(db, tmp_path) -> Path:
    src = tmp_path / "lib" / "Artist" / "Album" / "01 - Song.m4a"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"audio" * 100)
    src.with_suffix(".jpg").write_bytes(b"jpg")
    src.with_suffix(".lrc").write_bytes(b"[00:01.00]la")
    async with db() as session:
        session.add(Track(title="Song", artist="Artist", album="Album", track_number=1, status="done",
                          file_path=str(src), format="m4a"))
        await session.commit()
    return src


async def _sync(client, card: Path) -> dict:
    start = await client.post("/api/device/sync/start", data={"device_path": str(card), "scope": "all"})
    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events[-1]


@pytest.mark.asyncio
async def test_the_first_sync_names_the_card_and_records_every_file(client, db, fake_device, tmp_path):
    src = await _seed(db, tmp_path)

    await _sync(client, fake_device)

    card_id = json.loads((fake_device / ".leasure-card.json").read_text())["card_id"]
    async with db() as session:
        copies = (await session.scalars(select(CardCopy).order_by(CardCopy.kind))).all()
    assert [(c.card_id, c.kind, c.rel_path) for c in copies] == [
        (card_id, "audio", "Artist/Album/01 - Song.m4a"),
        (card_id, "jpg", "Artist/Album/01 - Song.jpg"),
        (card_id, "lrc", "Artist/Album/01 - Song.lrc"),
    ]
    audio = copies[0]
    assert (audio.size, audio.mtime) == (src.stat().st_size, src.stat().st_mtime)
    assert audio.head_sha256 == hashlib.sha256(src.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_the_card_keeps_its_id_across_syncs(client, db, fake_device, tmp_path):
    await _seed(db, tmp_path)
    await _sync(client, fake_device)
    first = (fake_device / ".leasure-card.json").read_text()

    await _sync(client, fake_device)

    assert (fake_device / ".leasure-card.json").read_text() == first


@pytest.mark.asyncio
async def test_an_unchanged_sync_does_not_hash_again(client, db, fake_device, tmp_path, monkeypatch):
    await _seed(db, tmp_path)
    await _sync(client, fake_device)
    hashed = []
    real = device_router._head_sha256
    monkeypatch.setattr(device_router, "_head_sha256", lambda path: hashed.append(path) or real(path))

    done = await _sync(client, fake_device)

    assert done["skipped"] == 1 and hashed == []
    async with db() as session:
        assert len((await session.scalars(select(CardCopy))).all()) == 3


@pytest.mark.asyncio
async def test_a_copy_that_fails_midway_keeps_the_old_card_file(client, db, fake_device, tmp_path, monkeypatch):
    src = await _seed(db, tmp_path)
    await _sync(client, fake_device)
    card_audio = fake_device / "Artist" / "Album" / "01 - Song.m4a"
    before = card_audio.read_bytes()
    src.write_bytes(b"retagged" * 100)

    def copy_half_then_fail(a, b):
        Path(b).write_bytes(Path(a).read_bytes()[:10])
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(shutil, "copyfile", copy_half_then_fail)

    done = await _sync(client, fake_device)

    assert done["copied"] == 0 and done["errors"]
    assert card_audio.read_bytes() == before
    assert list(fake_device.rglob("*.leasure-part")) == []
