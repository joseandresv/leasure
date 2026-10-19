import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from models import SupersededFile, SyncHistory, Track
from routers import device as device_router


async def _add(db, src: Path, title: str, **fields) -> Track:
    async with db() as session:
        track = Track(title=title, artist="Artist", album="Album", track_number=1, status="done",
                      file_path=str(src), format=src.suffix.lstrip("."), **fields)
        session.add(track)
        await session.commit()
        return track


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x01" * size)
    return path


async def _sync(client, card: Path) -> dict:
    start = await client.post("/api/device/sync/start", data={"device_path": str(card), "scope": "all"})
    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events[-1]


def _free(monkeypatch, beyond_reserve: int, cluster: int = 1) -> None:
    monkeypatch.setattr(device_router, "_free_bytes", lambda target: device_router._CARD_RESERVE_BYTES + beyond_reserve)
    monkeypatch.setattr(device_router, "_cluster_bytes", lambda target: cluster)


async def _history_rows(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(SyncHistory))


@pytest.mark.asyncio
async def test_a_replacement_needs_its_full_size_before_the_old_copy_goes(client, db, fake_device, tmp_path, monkeypatch):
    src = _write(tmp_path / "lib" / "Song.m4a", 3000)
    await _add(db, src, "Song")
    await _sync(client, fake_device)
    card_copy = fake_device / "Artist" / "Album" / "01 - Song.m4a"
    _write(src, 6000)
    _free(monkeypatch, 5000)
    rows = await _history_rows(db)

    event = await _sync(client, fake_device)

    assert event["type"] == "error"
    assert event["message"].startswith("Not enough space on the card: this sync needs 64 MB and 64 MB is free.")
    assert card_copy.stat().st_size == 3000
    assert await _history_rows(db) == rows


@pytest.mark.asyncio
async def test_a_sync_that_fits_copies(client, db, fake_device, tmp_path, monkeypatch):
    await _add(db, _write(tmp_path / "lib" / "Song.m4a", 6000), "Song")
    _free(monkeypatch, 7000)

    done = await _sync(client, fake_device)

    assert (done["type"], done["copied"]) == ("done", 1)


@pytest.mark.asyncio
async def test_files_and_new_folders_take_whole_clusters_on_the_card(client, db, fake_device, tmp_path, monkeypatch):
    await _add(db, _write(tmp_path / "lib" / "Song.m4a", 1000), "Song")
    _free(monkeypatch, 3 * 32768 - 1, cluster=32768)

    event = await _sync(client, fake_device)

    assert event["type"] == "error" and event["message"].startswith("Not enough space on the card")
    assert not (fake_device / "Artist").exists()


@pytest.mark.asyncio
async def test_a_file_too_large_for_fat32_is_skipped(client, db, fake_device, tmp_path, monkeypatch):
    monkeypatch.setattr(device_router, "_MAX_CARD_FILE_BYTES", 1000)
    monkeypatch.setattr(device_router, "card_filesystem", lambda path: "fat")
    await _add(db, _write(tmp_path / "lib" / "Big.flac", 2000), "Big")

    done = await _sync(client, fake_device)

    assert done["errors"] == ["Big: too large for the card"] and done["copied"] == 0
    assert not (fake_device / "Artist").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fs", ["exfat", None])
async def test_a_file_over_the_fat32_limit_is_copied_to_a_stock_card_that_is_not_fat32(client, db, fake_device,
                                                                                      tmp_path, monkeypatch, fs):
    monkeypatch.setattr(device_router, "_MAX_CARD_FILE_BYTES", 1000)
    monkeypatch.setattr(device_router, "card_filesystem", lambda path: fs)
    await _add(db, _write(tmp_path / "lib" / "Big.flac", 2000), "Big")

    done = await _sync(client, fake_device)

    assert done["errors"] == [] and done["copied"] == 1


@pytest.mark.asyncio
async def test_a_path_too_long_for_windows_is_skipped(client, db, fake_device, tmp_path, monkeypatch):
    monkeypatch.setattr(device_router, "get_platform", lambda: "wsl2")
    # build_device_path caps card paths at 250 bytes now, so only a lower limit still trips the precheck.
    monkeypatch.setattr(device_router, "_MAX_CARD_PATH", 200)
    title = "T" * 195
    async with db() as session:
        session.add(Track(title=title, artist="A" * 40, album="B" * 40, track_number=1, status="done",
                          file_path=str(_write(tmp_path / "lib" / "Long.mp3", 10)), format="mp3"))
        await session.commit()

    done = await _sync(client, fake_device)

    assert done["errors"] == [f"{title}: path too long for the card"]
    assert list(fake_device.rglob("*.mp3")) == []


@pytest.fixture
def diff_context(monkeypatch):
    seen = {}
    real = device_router.templates.TemplateResponse

    def capture(*args, **kwargs):
        seen.update(kwargs.get("context") or {})
        return real(*args, **kwargs)

    monkeypatch.setattr(device_router.templates, "TemplateResponse", capture)
    return seen


@pytest.mark.asyncio
async def test_the_diff_shows_space_and_the_old_copies(client, db, fake_device, tmp_path, monkeypatch, diff_context):
    monkeypatch.setattr(device_router, "_MAX_CARD_FILE_BYTES", 5000)
    monkeypatch.setattr(device_router, "card_filesystem", lambda path: "fat")
    monkeypatch.setattr(device_router, "_cluster_bytes", lambda target: 1)
    old = _write(tmp_path / "lib" / "Song.m4a", 3000)
    track = await _add(db, old, "Song")
    await _add(db, _write(tmp_path / "lib" / "Huge.flac", 6000), "Huge")
    await _sync(client, fake_device)
    st = old.stat()
    flac = _write(old.with_suffix(".flac"), 4000)
    changed = _write(fake_device / "Artist" / "Album" / "02 - Changed.m4a", 10)
    async with db() as session:
        row = await session.get(Track, track.id)
        row.file_path, row.format = str(flac), "flac"
        session.add_all([
            SupersededFile(track_id=track.id, basis="library", kind="audio", rel_path="Artist/Album/01 - Song.m4a",
                           size=st.st_size, mtime=st.st_mtime, head_sha256=device_router._head_sha256(old)),
            SupersededFile(track_id=track.id, basis="library", kind="audio", rel_path="Artist/Album/02 - Changed.m4a",
                           size=10, mtime=changed.stat().st_mtime, head_sha256="0" * 64),
        ])
        await session.commit()

    await client.get("/api/device/diff/html", params={"device_path": str(fake_device)})

    space = diff_context["space"]
    assert (space["needed_bytes"], space["fits"]) == (4000 + device_router._CARD_RESERVE_BYTES, True)
    assert space["too_large"] == ["Artist/Album/01 - Huge.flac"] and space["too_long"] == []
    assert diff_context["to_remove"] == [{"path": "Artist/Album/01 - Song.m4a", "title": "Song", "artist": "Artist",
                                          "state": "waiting"}]
    assert diff_context["kept"] == [{"path": "Artist/Album/02 - Changed.m4a", "title": "Song", "artist": "Artist",
                                     "reason": "changed_on_card"}]
