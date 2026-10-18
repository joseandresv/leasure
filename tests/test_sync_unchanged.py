import json
import os

import pytest
from sqlalchemy import select

from models import SyncHistory, Track


async def _library_track(db, tmp_path, body: bytes = b"\xff\xfb\x90\x00" + b"\x00" * 413):
    src = tmp_path / "lib" / "Artist" / "Album" / "01 - Song.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)
    src.with_suffix(".jpg").write_bytes(b"jpg")
    async with db() as session:
        session.add(Track(title="Song", artist="Artist", album="Album", track_number=1, status="done",
                          file_path=str(src), format="mp3"))
        await session.commit()
    return src


async def _sync(client, fake_device) -> dict:
    start = await client.post("/api/device/sync/start", data={"device_path": str(fake_device), "scope": "all"})
    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events[-1]


@pytest.mark.asyncio
async def test_a_second_sync_skips_files_the_card_already_holds(client, db, fake_device, tmp_path):
    await _library_track(db, tmp_path)
    await _sync(client, fake_device)

    done = await _sync(client, fake_device)

    assert (done["synced"], done["copied"], done["skipped"], done["size_mb"]) == (1, 0, 1, 0)
    async with db() as session:
        history = (await session.scalars(select(SyncHistory).order_by(SyncHistory.id))).all()
    assert [(h.tracks_added, h.tracks_skipped) for h in history] == [(1, 0), (0, 1)]


@pytest.mark.asyncio
async def test_a_copy_carries_the_library_mtime(client, db, fake_device, tmp_path):
    src = await _library_track(db, tmp_path)
    os.utime(src, (1_700_000_000, 1_700_000_000))

    await _sync(client, fake_device)

    assert abs((fake_device / "Artist" / "Album" / "01 - Song.mp3").stat().st_mtime - 1_700_000_000) <= 2


@pytest.mark.asyncio
async def test_a_changed_library_file_is_copied_again(client, db, fake_device, tmp_path):
    src = await _library_track(db, tmp_path)
    await _sync(client, fake_device)
    src.write_bytes(src.read_bytes() + b"retagged")

    done = await _sync(client, fake_device)

    assert done["copied"] == 1
    assert (fake_device / "Artist" / "Album" / "01 - Song.mp3").read_bytes() == src.read_bytes()


@pytest.mark.asyncio
async def test_the_diff_counts_an_outdated_card_copy_as_to_add(client, db, fake_device, tmp_path):
    src = await _library_track(db, tmp_path)
    await _sync(client, fake_device)
    src.write_bytes(src.read_bytes() + b"retagged")

    diff = await client.get("/api/device/diff/html", params={"device_path": str(fake_device)})

    assert "<strong>1</strong> to add" in diff.text and "<strong>0</strong> already on device" in diff.text
