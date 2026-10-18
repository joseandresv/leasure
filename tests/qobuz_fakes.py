"""Shared fakes for the Qobuz engine, upgrade, routing and route tests: a scripted client,
header-only FLAC files and a stored sign-in. No network, no real Qobuz."""

import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

from services import qobuz_client, qobuz_session
from services.qobuz_client import DownloadedFile, FileUrl, QobuzTrack

TOKEN = "tok_" + "A1b2C3d4E5f6G7h8" * 2


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    local = Path.home() / ".local" / "bin" / "ffmpeg"
    return found or (str(local) if local.exists() else None)


def qtrack(**overrides) -> QobuzTrack:
    fields = {"id": "q1", "title": "So What", "version": None, "performer": "Miles Davis",
              "album_title": "Kind of Blue", "album_id": "a1", "album_artist": "Miles Davis", "album_upc": None,
              "isrc": "USSM15900113", "duration_s": 200, "track_number": 1, "media_number": 1,
              "max_bit_depth": 24, "max_sampling_rate": 96.0, "streamable": True, "hires_streamable": True,
              "cover_url": None}
    fields.update(overrides)
    return QobuzTrack(**fields)


def make_flac(path: Path, sample_rate: int = 96000, bits: int = 24, seconds: float = 200.0, channels: int = 2,
              pad: bool = True) -> Path:
    """A STREAMINFO-only FLAC (no frames). `pad` appends filler so the bitrate floor passes;
    only for tests that patch out the full decode."""
    total = int(sample_rate * seconds)
    body = struct.pack(">HH", 4096, 4096) + bytes(6)
    body += ((sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | total).to_bytes(8, "big")
    body += bytes(16)
    data = b"fLaC" + bytes([0x80]) + len(body).to_bytes(3, "big") + body
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
        if pad:
            handle.truncate(len(data) + int(seconds * 130000 * channels / 8))
    return path


@dataclass
class FakeClient:
    """Answers like QobuzClient from scripted tracks; download() writes a header-only FLAC."""
    search: list = field(default_factory=lambda: [qtrack()])
    by_isrc: list = field(default_factory=list)
    by_id: dict = field(default_factory=dict)
    flac: dict = field(default_factory=lambda: {"sample_rate": 96000, "bits": 24, "seconds": 200.0})
    format_id: int = 7
    errors: dict = field(default_factory=dict)       # track id -> exception raised once the body starts
    resolve_errors: dict = field(default_factory=dict)  # track id -> API error raised before the body (no cap slot)
    download_calls: list = field(default_factory=list)
    rotated_token: str | None = None
    token_fingerprint: str = field(default_factory=lambda: qobuz_session.token_fingerprint(TOKEN))
    closed: bool = False
    writer: object = None                            # optional callable(dest, max_format_id)

    def search_tracks(self, query, limit=10):
        return list(self.search)

    def find_by_isrc(self, isrc, limit=10):
        return list(self.by_isrc)

    def get_track(self, track_id):
        if track_id not in self.by_id:
            raise qobuz_client.QobuzNotFoundError("Qobuz has no such track.")
        return self.by_id[track_id]

    def download(self, track_id, dest, *, max_format_id=27, max_sampling_khz=192.0, expected_duration_s=None,
                 before_body=None, progress=None):
        self.download_calls.append((track_id, max_format_id))
        if track_id in self.resolve_errors:
            raise self.resolve_errors[track_id]
        stream = FileUrl(url="https://x.akamaized.net/f", format_id=self.format_id, requested_format_id=max_format_id,
                         mime_type="audio/flac", bit_depth=self.flac["bits"],
                         sampling_rate=self.flac["sample_rate"] / 1000, duration_s=int(self.flac["seconds"]))
        if before_body:
            before_body(stream, None)
        if track_id in self.errors:
            raise self.errors[track_id]
        if self.writer:
            self.writer(dest, max_format_id)
        else:
            make_flac(dest, **self.flac)
        return DownloadedFile(path=Path(dest), size=Path(dest).stat().st_size, content_length=None, stream=stream)

    def close(self):
        self.closed = True


def store_session(**overrides) -> qobuz_session.StoredSession:
    fields = {"user_id": "12344321", "user_auth_token": TOKEN, "app_id": "123456789",
              "issued_at": "2026-10-18T10:00:00+00:00", **overrides}
    session = qobuz_session.StoredSession(**fields)
    qobuz_session.save(session)
    return session


async def seed_track(db, **overrides) -> int:
    from models import Track

    fields = {"title": "So What", "artist": "Miles Davis", "album": "Kind of Blue", "track_number": 1,
              "duration_ms": 200_000, "spotify_uri": None, "youtube_id": "yt1", "quality": "native",
              "status": "pending"}
    fields.update(overrides)
    async with db() as session:
        track = Track(**fields)
        session.add(track)
        await session.commit()
        return track.id


async def get_track(db, track_id):
    from models import Track

    async with db() as session:
        return await session.get(Track, track_id)
