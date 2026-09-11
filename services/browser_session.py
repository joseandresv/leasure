"""The YouTube session of the browser window Leasure was launched in.

Chrome 127+ app-bound encryption puts the user's normal profile out of yt-dlp's reach, so
`scripts/launch.ps1` opens Leasure in a Chrome/Edge `--app` window with its own profile and
a loopback DevTools port, and records it in `data/browser-cdp.json`:

    {"port": 9222, "pid": 1234, "browser": "chrome", "started_at": "<iso>"}

Reading the cookies out of that window over CDP is what replaces the manual header paste.
When the server runs inside WSL2 with NAT networking it cannot reach that port on Windows
loopback at all, so the launcher reads the cookies itself and relays them through
`data/browser-cookies.json` (`{"fetched_at", "browser", "cookies": [...]}`), refreshing it on
request via a `data/browser-cookies.request` marker.

Everything here is blocking (HTTP + WebSocket + file polling); call it from `asyncio.to_thread`.
"""

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from config import settings
from services import cookies

logger = logging.getLogger(__name__)

CDP_CONNECT_TIMEOUT = 2.0
CDP_REPLY_TIMEOUT = 10.0
# The launcher re-relays every 5 minutes, so anything older than this means it is gone.
RELAY_MAX_AGE_SECONDS = 600
COOKIE_DOMAINS = (".youtube.com", ".google.com")
# The cookie that proves a signed-in Google session; __Secure-3PAPISID is its third-party
# copy and carries the same value, so either one is enough.
SESSION_COOKIE_NAMES = ("SAPISID", "__Secure-3PAPISID")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

NO_WINDOW_MESSAGE = ("Leasure is not running in its own browser window, so there is no login to read. "
                     "Start it from the desktop shortcut, or paste your headers below.")
NOT_SIGNED_IN_MESSAGE = ("This window is not signed in to YouTube Music yet. Open music.youtube.com in it, "
                         "sign in, then use this button again.")

_import_lock = threading.Lock()


def cdp_file_path() -> Path:
    return settings.data_dir / "browser-cdp.json"


def cdp_info() -> dict | None:
    """The app window's live DevTools endpoint, or None when there is no such window."""
    path = cdp_file_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Cannot read the app window record %s: %s", path, e)
        return None

    port = record.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        logger.warning("Ignoring app window record with port %r", port)
        return None

    try:
        response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=CDP_CONNECT_TIMEOUT)
        response.raise_for_status()
        version = response.json()
    except Exception as e:
        logger.debug("No app window answering on port %s: %s", port, str(e)[:200])
        return None

    ws_url = version.get("webSocketDebuggerUrl") or ""
    if not _is_loopback(ws_url, port):
        logger.warning("App window on port %s reported a debugger URL we will not open: %s", port, ws_url[:120])
        return None

    return {"port": port, "pid": record.get("pid"), "browser": record.get("browser"),
            "started_at": record.get("started_at"), "ws_url": ws_url,
            "browser_version": version.get("Browser")}


def _is_loopback(ws_url: str, expected_port: int) -> bool:
    parsed = urlparse(ws_url)
    return parsed.scheme == "ws" and parsed.hostname in _LOOPBACK_HOSTS and parsed.port == expected_port


def _domain_matches(domain: str, suffixes: tuple[str, ...]) -> bool:
    host = (domain or "").lower().lstrip(".")
    return any(host == suffix.lstrip(".") or host.endswith(suffix if suffix.startswith(".") else f".{suffix}")
               for suffix in suffixes)


def relay_file_path() -> Path:
    return settings.data_dir / "browser-cookies.json"


def relay_request_path() -> Path:
    return settings.data_dir / "browser-cookies.request"


def _relay_record() -> dict | None:
    path = relay_file_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Cannot read the relayed cookies at %s: %s", path, e)
        return None
    return record if isinstance(record, dict) else None


def _relay_age_seconds(record: dict) -> float | None:
    try:
        fetched_at = datetime.fromisoformat(record["fetched_at"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Relayed cookies without a usable fetched_at: %r", record.get("fetched_at"))
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - fetched_at).total_seconds()


def relay_cookies(max_age_seconds: float = RELAY_MAX_AGE_SECONDS) -> list[dict] | None:
    """The cookies the launcher relayed through a file, or None when there is no fresh relay."""
    record = _relay_record()
    if record is None:
        return None
    age = _relay_age_seconds(record)
    if age is None or age > max_age_seconds:
        logger.debug("Ignoring relayed cookies: age %s s", age)
        return None
    relayed = record.get("cookies")
    if not isinstance(relayed, list):
        logger.warning("Relayed cookie file has no cookie list")
        return None
    return [c for c in relayed if isinstance(c, dict) and _domain_matches(c.get("domain", ""), COOKIE_DOMAINS)]


def request_relay_refresh(timeout: float = 10) -> bool:
    """Ask the launcher to relay the window's cookies again; True once the file has advanced."""
    before = (_relay_record() or {}).get("fetched_at")
    path = relay_request_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as e:
        logger.warning("Could not ask the launcher for fresh cookies (%s): %s", path, e)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if (_relay_record() or {}).get("fetched_at") not in (None, before):
            return True
    logger.warning("The launcher did not relay fresh cookies within %s s", timeout)
    return False


def _cookies_with_source() -> tuple[str | None, list[dict]]:
    if cdp_info():
        return "cdp", get_browser_cookies()
    relayed = relay_cookies()
    return ("relay", relayed) if relayed is not None else (None, [])


def get_session_cookies() -> list[dict]:
    """The app window's cookies, read over CDP when its port answers and from the relay otherwise."""
    return _cookies_with_source()[1]


def session_status() -> dict:
    """Whether Leasure can read the app window's login, over which channel, and if it is signed in."""
    via, browser_cookies = _cookies_with_source()
    return {"app_window": via is not None, "logged_in": is_logged_in(browser_cookies) if via else None, "via": via}


def get_browser_cookies(domains: tuple[str, ...] = COOKIE_DOMAINS) -> list[dict]:
    """The app window's cookies for `domains`, as CDP cookie dicts. Blocking."""
    info = cdp_info()
    if not info:
        return []
    try:
        reply = _cdp_call(info["ws_url"], "Storage.getCookies")
    except Exception as e:
        logger.warning("Could not read cookies from the app window: %s", str(e)[:200])
        return []
    if reply.get("error"):
        logger.warning("The app window refused Storage.getCookies: %s", reply["error"])
        return []
    return [c for c in reply.get("result", {}).get("cookies", []) if _domain_matches(c.get("domain", ""), domains)]


def _cdp_call(ws_url: str, method: str, request_id: int = 1) -> dict:
    from websockets.sync.client import connect

    with connect(ws_url, open_timeout=CDP_CONNECT_TIMEOUT, close_timeout=CDP_CONNECT_TIMEOUT,
                 max_size=64 * 1024 * 1024) as ws:
        ws.send(json.dumps({"id": request_id, "method": method}))
        deadline = time.monotonic() + CDP_REPLY_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"The app window did not answer {method}")
            message = json.loads(ws.recv(timeout=remaining))
            if message.get("id") == request_id:
                return message


def is_logged_in(browser_cookies: list[dict]) -> bool:
    """Whether the window's cookies carry a signed-in Google session."""
    return any(c.get("name") in SESSION_COOKIE_NAMES and c.get("value")
               and _domain_matches(c.get("domain", ""), COOKIE_DOMAINS) for c in browser_cookies)


def _header_pairs(browser_cookies: list[dict]) -> list[tuple[str, str]]:
    """name/value pairs for a music.youtube.com request; the .youtube.com copy wins."""
    chosen: dict[str, str] = {}
    for suffix in COOKIE_DOMAINS:
        for cookie in browser_cookies:
            if cookie.get("name") and cookie.get("value") and _domain_matches(cookie.get("domain", ""), (suffix,)):
                chosen.setdefault(cookie["name"], cookie["value"])
    return list(chosen.items())


def _cookies_to_import() -> tuple[str | None, list[dict]]:
    """Cookies for an import: over CDP when reachable, else a relay refreshed first so a
    login made seconds ago is already in the file."""
    if cdp_info():
        return "cdp", get_browser_cookies()
    if _relay_record() is None:
        return None, []
    request_relay_refresh()
    relayed = relay_cookies()
    return ("relay", relayed) if relayed is not None else (None, [])


def import_session(run_premium_check: bool = True) -> dict:
    """Read the app window's login into cookies.txt and the ytmusicapi headers. Blocking."""
    from services.youtube_client import write_headers_from_cookie_pairs

    result = {"ok": False, "logged_in": False, "cookies": 0, "premium": None, "error": None}
    via, browser_cookies = _cookies_to_import()
    if via is None:
        result["error"] = NO_WINDOW_MESSAGE
        return result

    result["cookies"] = len(browser_cookies)
    result["logged_in"] = is_logged_in(browser_cookies)
    if not result["logged_in"]:
        result["error"] = NOT_SIGNED_IN_MESSAGE
        return result

    if not cookies.write_cookie_file(browser_cookies, source="app-window"):
        result["error"] = "The window's cookies held no usable YouTube session."
        return result
    if not write_headers_from_cookie_pairs(_header_pairs(browser_cookies)):
        result["error"] = "Saved the cookies, but could not build the YouTube Music API headers."
        return result

    result["ok"] = True
    logger.info("Imported the YouTube session from the app window (via %s)", via)
    if run_premium_check:
        result["premium"] = cookies.premium_check()
    return result


def _is_usable(status: dict, max_age_hours: float) -> bool:
    return bool(status["exists"] and status["source"] == "app-window"
                and (status["age_hours"] or 0) <= max_age_hours)


def ensure_fresh_cookies(max_age_hours: float = 6) -> bool:
    """Re-import from the app window when the cookie file is missing, stale or from elsewhere.

    Runs inside download threads, so a failure is logged and the existing file is used."""
    if _is_usable(cookies.cookie_file_status(), max_age_hours):
        return True
    try:
        if cdp_info() or relay_cookies() is not None:
            with _import_lock:
                if not _is_usable(cookies.cookie_file_status(), max_age_hours):
                    outcome = import_session(run_premium_check=False)
                    if outcome["error"]:
                        logger.warning("App window cookie refresh: %s", outcome["error"])
    except Exception as e:
        logger.warning("App window cookie refresh failed: %s", str(e)[:200])
    return cookies.cookie_file_status()["exists"]
