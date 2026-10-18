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
os.environ["ALLOWED_HOSTS"] = '["127.0.0.1","localhost","test"]'

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # Jinja2Templates(directory="templates") and StaticFiles are relative

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402


def _inside(path: Path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def assert_isolated_paths(db_file: Path | None = None) -> None:
    """Stop the run unless the settings dirs (and the database file) are temp paths outside the repo."""
    from config import settings

    temp = Path(tempfile.gettempdir())
    paths = {name: getattr(settings, name) for name in ("data_dir", "library_dir", "download_dir")}
    if db_file is not None:
        paths["database"] = db_file
    for name, path in paths.items():
        allowed = _TMP if name == "database" else temp
        if not _inside(path, allowed) or _inside(path, ROOT):
            pytest.exit(f"Refusing to run: {name} is {Path(path).resolve()}, outside the test temp dir {allowed}. "
                        "Tests empty the database and write under these dirs; run pytest from the repo root so "
                        "tests/conftest.py sets DATA_DIR/LIBRARY_DIR/DOWNLOAD_DIR before config is imported.",
                        returncode=2)


@pytest.fixture(autouse=True)
def isolated_paths():
    """Checks every test's paths; fixtures that repoint settings call the returned guard again."""
    assert_isolated_paths()
    return assert_isolated_paths


@pytest.fixture(autouse=True)
def qobuz_isolation(monkeypatch):
    """No Qobuz state leaks between tests: fresh client/engine/session state, no throttling,
    no track gap, no stored sign-in or counters, and icacls never really runs."""
    from config import settings
    from services import qobuz_client, qobuz_engine, qobuz_session, secret_file

    if qobuz_client.IMPLEMENTED:
        qobuz_client._reset_state_for_tests()
        monkeypatch.setattr(qobuz_client, "shared_throttle",
                            lambda _cpm: qobuz_client.Throttle(1e9, sleep=lambda _s: None))
    qobuz_engine._reset_for_tests()
    qobuz_session._reset_for_tests()
    monkeypatch.setattr(qobuz_engine, "TRACK_GAP_S", (0, 0))
    calls: list[list[str]] = []

    def record(args):
        calls.append(list(args))
        return 0, ""

    monkeypatch.setattr(secret_file, "_run", record)
    for name in ("qobuz_session.json", "qobuz_check.json", "qobuz_daily.json", "qobuz_app.json"):
        (settings.data_dir / name).unlink(missing_ok=True)
    return calls


@pytest.fixture(autouse=True)
def fresh_download_worker():
    """The singleton worker starts each test with an empty queue and nothing deferred."""
    from worker import download_worker

    download_worker.__init__()
    return download_worker


@pytest_asyncio.fixture
async def db():
    """Fresh schema; tables are emptied after each test."""
    from db import async_session, engine, init_db
    from models import CardCopy, Playlist, PlaylistTrack, SupersededFile, SyncHistory, Track

    db_file = Path(engine.url.database)
    assert_isolated_paths(db_file)
    await init_db()
    yield async_session
    assert_isolated_paths(db_file)
    async with async_session() as session:
        for model in (PlaylistTrack, Playlist, SyncHistory, CardCopy, SupersededFile, Track):
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
