"""Test bootstrap: point every on-disk path at a temp directory *before* the app
modules import `config` (which creates the directories at import time)."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="leasure-test-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["LIBRARY_DIR"] = str(_TMP / "library")
os.environ["DOWNLOAD_DIR"] = str(_TMP / "downloads")
os.environ["SPOTIFY_CLIENT_ID"] = ""
os.environ["SPOTIFY_CLIENT_SECRET"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # Jinja2Templates(directory="templates") and StaticFiles are relative

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402


@pytest_asyncio.fixture
async def db():
    """Fresh schema; tables are emptied after each test."""
    from db import async_session, init_db
    from models import Playlist, PlaylistTrack, SyncHistory, Track

    await init_db()
    yield async_session
    async with async_session() as session:
        for model in (PlaylistTrack, Playlist, SyncHistory, Track):
            await session.execute(delete(model))
        await session.commit()


@pytest_asyncio.fixture
async def client(db):
    from app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def fake_device(tmp_path, monkeypatch):
    """A temp directory that detect_devices() reports as the only (player) volume."""
    root = tmp_path / "H2"
    root.mkdir()
    fake = [{"path": str(root), "drive_letter": "H", "label": "H2", "total_gb": 32.0,
             "free_gb": 30.0, "used_gb": 2.0, "has_music_files": True, "device_type": "player", "file_count": 0}]
    import services.device as device_mod
    monkeypatch.setattr(device_mod, "detect_devices", lambda: list(fake))
    return root
