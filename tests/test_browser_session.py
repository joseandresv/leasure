import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from config import settings
from services import browser_session, cookies
from services import youtube_client as yt

WINDOW = {"port": 9222, "pid": 4242, "browser": "chrome", "started_at": "2026-09-10T10:00:00+00:00",
          "ws_url": "ws://127.0.0.1:9222/devtools/browser/abc", "browser_version": "Chrome/146"}

SIGNED_IN_COOKIES = [
    {"name": "SAPISID", "value": "sapi", "domain": ".youtube.com", "path": "/", "expires": 2000000000, "secure": True},
    {"name": "LOGIN_INFO", "value": "login", "domain": ".youtube.com", "path": "/", "expires": -1, "secure": False},
    {"name": "__Secure-3PAPISID", "value": "sapi", "domain": ".google.com", "path": "/", "expires": 0, "secure": True},
    {"name": "DEVICE_INFO", "value": "junk", "domain": ".youtube.com", "path": "/", "expires": 0, "secure": True},
]
SIGNED_OUT_COOKIES = [c for c in SIGNED_IN_COOKIES if c["name"] not in browser_session.SESSION_COOKIE_NAMES]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_file", None)
    monkeypatch.setattr(yt, "HEADERS_PATH", tmp_path / "youtube_headers.json")
    return tmp_path


@pytest.fixture
def app_window(monkeypatch):
    """A signed-in app window whose cookies come from the fixture, not a real browser."""
    monkeypatch.setattr(browser_session, "cdp_info", lambda: dict(WINDOW))
    monkeypatch.setattr(browser_session, "get_browser_cookies", lambda *a, **k: list(SIGNED_IN_COOKIES))


@pytest.fixture
def no_premium_check(monkeypatch):
    calls = []
    monkeypatch.setattr(cookies, "premium_check", lambda *a, **k: calls.append(1) or {"state": "pass"})
    return calls


def test_import_session_writes_the_cookie_file_from_the_window(data_dir, app_window, no_premium_check):
    result = browser_session.import_session()

    assert (result["ok"], result["logged_in"], result["cookies"]) == (True, True, 4)
    lines = cookies.cookie_file_path().read_text().splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    assert lines[1] == ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\tsapi"
    # No expiry of its own: the default 180 days, and the browser's secure flag is kept.
    domain, subdomains, path, secure, expiry, name, value = lines[2].split("\t")
    assert (domain, secure, name, value) == (".youtube.com", "FALSE", "LOGIN_INFO", "login")
    assert int(expiry) > time.time() + 179 * 86400
    assert "DEVICE_INFO" not in cookies.cookie_file_path().read_text()


def test_import_session_writes_api_headers_and_runs_the_premium_check(data_dir, app_window, no_premium_check):
    result = browser_session.import_session()

    headers = json.loads(yt.HEADERS_PATH.read_text())
    assert headers["cookie"].startswith("SAPISID=sapi; ")
    assert headers["authorization"].startswith("SAPISIDHASH ")
    assert no_premium_check == [1] and result["premium"] == {"state": "pass"}
    assert cookies.cookie_file_status()["source"] == "app-window"


def test_import_session_refuses_a_window_that_is_not_signed_in(data_dir, monkeypatch, no_premium_check):
    monkeypatch.setattr(browser_session, "cdp_info", lambda: dict(WINDOW))
    monkeypatch.setattr(browser_session, "get_browser_cookies", lambda *a, **k: list(SIGNED_OUT_COOKIES))

    result = browser_session.import_session()

    assert result["ok"] is False and result["logged_in"] is False
    assert "sign in" in result["error"].lower()
    assert not cookies.cookie_file_path().exists() and no_premium_check == []


def test_import_session_without_an_app_window(data_dir, monkeypatch):
    monkeypatch.setattr(browser_session, "cdp_info", lambda: None)

    result = browser_session.import_session()

    assert result == {"ok": False, "logged_in": False, "cookies": 0, "premium": None,
                      "error": browser_session.NO_WINDOW_MESSAGE}


def test_ensure_fresh_cookies_reimports_a_stale_file_without_a_premium_check(data_dir, app_window, no_premium_check):
    cookies.write_cookie_file(SIGNED_IN_COOKIES, source="app-window")
    stale = time.time() - 8 * 3600
    os.utime(cookies.cookie_file_path(), (stale, stale))

    assert browser_session.ensure_fresh_cookies() is True
    assert cookies.cookie_file_status()["age_hours"] < 1
    assert no_premium_check == []


def test_ensure_fresh_cookies_keeps_a_fresh_import_untouched(data_dir, app_window, monkeypatch):
    cookies.write_cookie_file(SIGNED_IN_COOKIES, source="app-window")
    monkeypatch.setattr(browser_session, "import_session", lambda **k: pytest.fail("re-imported a fresh file"))

    assert browser_session.ensure_fresh_cookies() is True


def test_ensure_fresh_cookies_without_a_window_reports_the_existing_file(data_dir, monkeypatch):
    monkeypatch.setattr(browser_session, "cdp_info", lambda: None)

    assert browser_session.ensure_fresh_cookies() is False

    cookies.write_cookie_file(SIGNED_IN_COOKIES, source="headers")
    assert browser_session.ensure_fresh_cookies() is True


def test_cdp_info_is_none_without_a_record(data_dir):
    assert browser_session.cdp_info() is None


@pytest.mark.parametrize("port", [80, 70000, "9222", True])
def test_cdp_info_rejects_a_port_it_will_not_dial(data_dir, port):
    browser_session.cdp_file_path().write_text(json.dumps({"port": port, "pid": 1}))

    assert browser_session.cdp_info() is None


def test_get_browser_cookies_reads_them_over_cdp_and_filters_domains(monkeypatch):
    serve = pytest.importorskip("websockets.sync.server").serve
    payload = SIGNED_IN_COOKIES + [{"name": "SID", "value": "elsewhere", "domain": ".example.com"}]

    def handler(connection):
        for message in connection:
            request = json.loads(message)
            connection.send(json.dumps({"method": "Storage.cacheStorageListUpdated"}))  # an unrelated event first
            connection.send(json.dumps({"id": request["id"], "result": {"cookies": payload}}))

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setattr(browser_session, "cdp_info",
                            lambda: {**WINDOW, "port": port, "ws_url": f"ws://127.0.0.1:{port}/devtools/browser/x"})

        found = browser_session.get_browser_cookies()

    assert [c["name"] for c in found] == ["SAPISID", "LOGIN_INFO", "__Secure-3PAPISID", "DEVICE_INFO"]


async def test_session_status_route_reports_no_window(client, monkeypatch):
    from routers import youtube as youtube_router

    monkeypatch.setattr(youtube_router.browser_session, "cdp_info", lambda: None)

    body = (await client.get("/api/youtube/session/status")).json()

    assert body["app_window"] is False and body["logged_in"] is None
    assert set(body["cookie"]) == {"exists", "path", "age_hours", "stale", "source"}


async def test_session_import_route_returns_the_refreshed_card(client, monkeypatch):
    from routers import youtube as youtube_router

    monkeypatch.setattr(youtube_router.browser_session, "import_session",
                        lambda: {"ok": True, "logged_in": True, "cookies": 4, "premium": {"state": "pass"},
                                 "error": None})
    monkeypatch.setattr(youtube_router.browser_session, "cdp_info", lambda: dict(WINDOW))
    monkeypatch.setattr(youtube_router.browser_session, "get_browser_cookies", lambda *a, **k: list(SIGNED_IN_COOKIES))
    monkeypatch.setattr(youtube_router.yt, "is_connected", lambda: True)

    response = await client.post("/api/youtube/session/import", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="yt-session-card"' in response.text and "Connected" in response.text


async def test_session_import_route_shows_the_failure_on_the_card(client, monkeypatch):
    from routers import youtube as youtube_router

    monkeypatch.setattr(youtube_router.browser_session, "import_session",
                        lambda: {"ok": False, "logged_in": False, "cookies": 0, "premium": None,
                                 "error": browser_session.NOT_SIGNED_IN_MESSAGE})
    monkeypatch.setattr(youtube_router.browser_session, "cdp_info", lambda: dict(WINDOW))
    monkeypatch.setattr(youtube_router.browser_session, "get_browser_cookies", lambda *a, **k: list(SIGNED_OUT_COOKIES))
    monkeypatch.setattr(youtube_router.yt, "is_connected", lambda: False)

    response = await client.post("/api/youtube/session/import", headers={"HX-Request": "true"})

    assert "Sign in inside Leasure" in response.text
    assert browser_session.NOT_SIGNED_IN_MESSAGE[:40] in response.text


async def test_status_card_hints_at_the_shortcut_without_an_app_window(client, monkeypatch):
    from routers import youtube as youtube_router

    monkeypatch.setattr(youtube_router.browser_session, "cdp_info", lambda: None)
    monkeypatch.setattr(youtube_router.yt, "is_connected", lambda: False)

    response = await client.get("/api/youtube/status/html")

    assert "desktop shortcut" in response.text and "Sign in inside Leasure" not in response.text


def write_relay(age_seconds: float = 0, relayed=None):
    """The file scripts/launch.ps1 writes when the server cannot reach the DevTools port itself."""
    fetched_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    browser_session.relay_file_path().write_text(json.dumps({
        "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "browser": "chrome",
        "cookies": SIGNED_IN_COOKIES if relayed is None else relayed,
    }))


def test_relay_cookies_are_used_while_the_launcher_keeps_them_fresh(data_dir):
    write_relay()

    assert [c["name"] for c in browser_session.relay_cookies()] == [c["name"] for c in SIGNED_IN_COOKIES]
    assert browser_session.session_status() == {"app_window": True, "logged_in": True, "via": "relay"}


def test_relay_cookies_older_than_the_launcher_interval_are_ignored(data_dir):
    write_relay(age_seconds=browser_session.RELAY_MAX_AGE_SECONDS + 60)

    assert browser_session.relay_cookies() is None
    assert browser_session.session_status() == {"app_window": False, "logged_in": None, "via": None}


def test_session_status_prefers_the_window_over_the_relay(data_dir, app_window):
    write_relay(relayed=[])

    assert browser_session.session_status() == {"app_window": True, "logged_in": True, "via": "cdp"}


def test_request_relay_refresh_waits_for_the_launcher_to_rewrite_the_file(data_dir):
    write_relay(age_seconds=30)

    def serve_request():
        while not browser_session.relay_request_path().exists():
            time.sleep(0.05)
        write_relay()
        browser_session.relay_request_path().unlink()

    threading.Thread(target=serve_request, daemon=True).start()

    assert browser_session.request_relay_refresh(timeout=5) is True


def test_request_relay_refresh_gives_up_without_a_launcher(data_dir):
    write_relay(age_seconds=30)

    assert browser_session.request_relay_refresh(timeout=0.5) is False
    assert browser_session.relay_request_path().exists()


def test_import_session_through_the_relay_writes_the_cookie_file(data_dir, no_premium_check):
    write_relay()

    result = browser_session.import_session()

    assert (result["ok"], result["logged_in"], result["cookies"]) == (True, True, 4)
    assert cookies.cookie_file_status()["source"] == "app-window"
    assert "SAPISID=sapi; " in json.loads(yt.HEADERS_PATH.read_text())["cookie"]


def test_ensure_fresh_cookies_reimports_from_the_relay(data_dir, no_premium_check):
    cookies.write_cookie_file(SIGNED_IN_COOKIES, source="app-window")
    stale = time.time() - 8 * 3600
    os.utime(cookies.cookie_file_path(), (stale, stale))
    write_relay()

    assert browser_session.ensure_fresh_cookies() is True
    assert cookies.cookie_file_status()["age_hours"] < 1


async def test_session_status_route_reports_the_relay_as_the_source(client, data_dir, monkeypatch):
    from routers import youtube as youtube_router

    monkeypatch.setattr(youtube_router.browser_session, "cdp_info", lambda: None)
    write_relay()

    body = (await client.get("/api/youtube/session/status")).json()

    assert (body["app_window"], body["via"], body["logged_in"]) == (True, "relay", True)
