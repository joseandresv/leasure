"""Shared fakes for the Rockbox card tests: Rockbox's system folder with decoy files, a digest
that proves a folder is left byte-identical, library tracks, playlists and a sync helper."""

import hashlib
import io
import json
from pathlib import Path

from PIL import Image

from models import Playlist, PlaylistTrack, Track

# Decoys a careless sweep, scan or import would pick up: audio, a playlist and a cover.
ROCKBOX_FILES = {
    "rockbox.conf": b"volume: -20\n",
    "codecs/mpa.codec": b"\x7fELF codec",
    "database_0.tcd": b"TCDB",
    "x.mp3": b"\xff\xfb decoy audio",
    "x.m3u8": b"/Artist/Album/01 - Song.mp3\n",
    "a/b/cover.jpg": b"decoy jpg",
}


def add_rockbox(root: Path, name: str = ".rockbox") -> Path:
    for rel, data in ROCKBOX_FILES.items():
        path = root / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root / name


def tree_digest(path: Path) -> list[tuple[str, int, str]]:
    """Every file and folder under `path`: (rel, size, sha256); folders have size -1."""
    out = []
    for p in sorted(path.rglob("*")):
        rel = p.relative_to(path).as_posix()
        if p.is_dir():
            out.append((rel, -1, ""))
        else:
            out.append((rel, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()))
    return out


def jpeg(side: int = 800, progressive: bool = False, color=(200, 30, 30), height: int | None = None) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (side, height or side), color).save(buf, "JPEG", quality=85, progressive=progressive)
    return buf.getvalue()


async def seed_track(db, tmp_path: Path, *, title: str = "Song", artist: str = "Artist", album: str = "Album",
                     number: int = 1, ext: str = "mp3", art: bytes | None = b"jpg", body: bytes | None = None) -> Track:
    src = tmp_path / "lib" / artist / album / f"{number:02d} - {title}.{ext}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body if body is not None else b"\xff\xfb\x90\x00" + title.encode() * 50)
    if art is not None:
        src.with_suffix(".jpg").write_bytes(art)
    async with db() as session:
        track = Track(title=title, artist=artist, album=album, track_number=number, status="done",
                      file_path=str(src), format=ext)
        session.add(track)
        await session.commit()
    return track


async def add_playlist(db, name: str, tracks: list[Track]) -> None:
    async with db() as session:
        playlist = Playlist(name=name, source="manual")
        session.add(playlist)
        await session.flush()
        session.add_all(PlaylistTrack(playlist_id=playlist.id, track_id=t.id, position=i) for i, t in enumerate(tracks))
        await session.commit()


async def sync(client, card: Path, scope: str = "all") -> dict:
    """Start a sync, stream it to the end and return the last event."""
    start = await client.post("/api/device/sync/start", data={"device_path": str(card), "scope": scope})
    events = []
    async with client.stream("GET", f"/api/device/sync/stream/{start.json()['job_id']}") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events[-1]


def capture_contexts(monkeypatch) -> list[dict]:
    """Record the context of every partial routers.device renders."""
    from routers import device

    seen: list[dict] = []
    original = device.templates.TemplateResponse

    def recording(*args, **kwargs):
        seen.append({"name": kwargs.get("name"), **(kwargs.get("context") or {})})
        return original(*args, **kwargs)

    monkeypatch.setattr(device.templates, "TemplateResponse", recording)
    return seen
