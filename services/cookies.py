"""The YouTube cookie file yt-dlp downloads with, and the Premium gate that proves it works.

Premium quality (itag 141 AAC 256 / 774 Opus) is only served to a logged-in Premium
session on a music.youtube.com URL. A browser profile is not readable on every host
(WSL2 has none; Chrome 127+ app-bound encryption blocks extraction on Windows), so the
Netscape cookies.txt written here — from the login in Leasure's own browser window
(services/browser_session.py) or the manual header paste — is the
primary credential, and `premium_check()` makes "does Premium reach yt-dlp?" a fact.
"""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# The owner's Google/YouTube session cookies. Everything else in the pasted header
# (ads, experiments, analytics) is dropped so the file is the smallest copy of the
# session that still authenticates. `__Secure-*` is a prefix rule because Google keeps
# adding variants (__Secure-1PSID, __Secure-3PAPISID, __Secure-1PSIDTS, ...).
AUTH_COOKIE_NAMES = frozenset({
    "APISID",
    "CONSENT",
    "HSID",
    "LOGIN_INFO",
    "PREF",
    "SAPISID",
    "SID",
    "SIDCC",
    "SOCS",
    "SSID",
    "VISITOR_INFO1_LIVE",
    "VISITOR_PRIVACY_METADATA",
    "YSC",
})
AUTH_COOKIE_PREFIX = "__Secure-"

# These are set on .google.com as well as .youtube.com; yt-dlp needs both when a
# request is redirected through accounts.google.com.
_GOOGLE_DOMAIN_NAMES = frozenset({"APISID", "HSID", "LOGIN_INFO", "SAPISID", "SID", "SSID"})

STALE_AFTER_HOURS = 12
COOKIE_LIFETIME_DAYS = 180
PREMIUM_ITAGS = ("141", "774")

# Probe tracks get taken down (the old default did, mid-2026), which looks like a broken
# session on the card. When the configured one is gone, try these before giving up.
PREMIUM_CHECK_FALLBACK_IDS = ("kJQP7kiw5Fk",)
_UNAVAILABLE_MARKERS = ("video unavailable", "this video is not available", "private video", "has been removed")
_NO_JS_RUNTIME_MARKERS = ("signature solving failed", "challenge solving failed", "javascript runtime")

NO_COOKIE_MESSAGE = (
    "No cookie file yet: paste your browser headers in the YouTube Music card, "
    "or export cookies.txt from a browser logged into music.youtube.com and save it as {path}."
)

NO_FORMATS_MESSAGE = (
    "yt-dlp got no audio formats for this track. That is not a cookie problem: it almost always means the deno "
    "JavaScript runtime is missing, so yt-dlp cannot solve YouTube's signature challenge. Install it from "
    "https://deno.land (it lands in ~/.deno/bin) and run the check again."
)


def cookie_file_path() -> Path:
    # An empty COOKIE_FILE= in .env parses to Path("."), which is not a cookie file.
    if settings.cookie_file and settings.cookie_file != Path("."):
        return settings.cookie_file
    return settings.data_dir / "cookies.txt"


def _meta_path() -> Path:
    path = cookie_file_path()
    return path.with_name(path.name + ".meta.json")


def _cache_path() -> Path:
    return settings.data_dir / "premium_check.json"


def is_auth_cookie(name: str) -> bool:
    return name in AUTH_COOKIE_NAMES or name.startswith(AUTH_COOKIE_PREFIX)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError as e:
        logger.debug("Could not restrict permissions on %s: %s", tmp, e)
    os.replace(tmp, path)


def _file_fingerprint(path: Path) -> dict:
    data = path.read_bytes()
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _write_meta(source: str) -> None:
    """Stamp the meta file with who wrote cookies.txt and what its bytes were, so a file
    someone replaced by hand afterwards does not inherit our written_at."""
    meta = {"source": source, "written_at": datetime.now(UTC).isoformat()}
    try:
        meta |= _file_fingerprint(cookie_file_path())
    except OSError as e:
        logger.warning("Could not fingerprint the cookie file: %s", e)
    _write_atomic(_meta_path(), json.dumps(meta))


def note_cookie_file_rewritten() -> None:
    """Re-stamp the fingerprint, keeping source and written_at: yt-dlp saves the jar back
    to cookiefile after every run, and that rewrite is our own session, not a hand edit."""
    meta = _read_meta()
    if not meta.get("written_at"):
        return
    try:
        _write_atomic(_meta_path(), json.dumps({**meta, **_file_fingerprint(cookie_file_path())}))
    except OSError as e:
        logger.debug("Could not re-stamp the cookie meta: %s", e)


def _netscape_line(cookie: dict, default_expiry: int) -> str:
    domain = cookie["domain"]
    expires = int(cookie.get("expires") or 0)
    return "\t".join([
        domain,
        "TRUE" if domain.startswith(".") else "FALSE",
        "/",
        "TRUE" if cookie.get("secure", True) else "FALSE",
        str(expires if expires > 0 else default_expiry),
        cookie["name"],
        cookie["value"],
    ])


def write_cookie_file(session_cookies: list[dict], source: str) -> Path | None:
    """Write the Netscape cookies.txt yt-dlp authenticates with, from cookie dicts.

    Each dict needs `domain`, `name` and `value`; a positive `expires` (epoch seconds) and
    `secure` are used when the browser supplied them. `source` is recorded in the meta file
    and shown on the card ("headers", "app-window", "browser")."""
    default_expiry = int((datetime.now(UTC) + timedelta(days=COOKIE_LIFETIME_DAYS)).timestamp())
    lines: dict[tuple[str, str], str] = {}
    for cookie in session_cookies:
        name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
        if not name or not value or not domain or not is_auth_cookie(name):
            continue
        lines[(domain, name)] = _netscape_line(cookie, default_expiry)

    if not lines:
        logger.warning("No usable YouTube auth cookies to write (source %s)", source)
        return None

    path = cookie_file_path()
    _write_atomic(path, "# Netscape HTTP Cookie File\n" + "\n".join(lines.values()) + "\n")
    _write_meta(source)
    logger.info("Wrote %d YouTube cookie lines to %s (source %s)", len(lines), path, source)
    return path


def _header_cookies(cookie_header: str) -> list[dict]:
    """Cookie dicts from a pasted `cookie` header, which carries no expiry or secure flag."""
    session_cookies = []
    for pair in cookie_header.split("; "):
        name, sep, value = pair.strip().partition("=")
        if not sep or not is_auth_cookie(name):
            continue
        domains = [".youtube.com"]
        if name in _GOOGLE_DOMAIN_NAMES or name.startswith(AUTH_COOKIE_PREFIX):
            domains.append(".google.com")
        session_cookies += [{"domain": domain, "name": name, "value": value} for domain in domains]
    return session_cookies


def write_cookie_file_from_headers(headers_path: Path | None = None) -> Path | None:
    """Convert the `cookie` header stored by the manual paste flow into a cookies.txt."""
    from services.youtube_client import HEADERS_PATH

    headers_path = headers_path or HEADERS_PATH
    try:
        headers = json.loads(Path(headers_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Cannot read YouTube headers at %s: %s", headers_path, e)
        return None

    cookie_header = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
    return write_cookie_file(_header_cookies(cookie_header), source="headers") if cookie_header else None


def refresh_from_browser() -> bool:
    """Write the cookie file from a local browser profile, if one is readable."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser

        jar = extract_cookies_from_browser(settings.cookie_browser)
        if not len(jar):
            logger.warning("Browser %s has no cookies to extract", settings.cookie_browser)
            return False
        path = cookie_file_path()
        jar.save(str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _write_meta("browser")
        logger.info("Refreshed cookie file from %s", settings.cookie_browser)
        return True
    except Exception as e:
        logger.warning("Cookie refresh from %s failed: %s", settings.cookie_browser, str(e)[:300])
        return False


def _read_meta() -> dict:
    try:
        meta = json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _written_at_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        written_at = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Cookie meta has an unreadable written_at: %s", value[:60])
        return None
    return written_at.replace(tzinfo=written_at.tzinfo or UTC).timestamp()


def _meta_describes(path: Path, meta: dict) -> bool:
    """Whether the meta file still describes the cookie file on disk.

    A meta written before fingerprints existed carries none, and there is nothing to
    disprove: it is trusted, or every installed session would read as a stranger's file."""
    if "sha256" not in meta:
        return True
    try:
        return _file_fingerprint(path) == {"size": meta.get("size"), "sha256": meta.get("sha256")}
    except OSError:
        return False


def cookie_file_status() -> dict:
    """Whether a cookie file exists, how old it is, and where it came from."""
    path = cookie_file_path()
    if not path.exists():
        return {"exists": False, "path": str(path), "age_hours": None, "stale": True, "source": None}

    meta = _read_meta()
    if not _meta_describes(path, meta):
        meta = {}
    # yt-dlp saves the jar back to cookiefile after every run, so the mtime dates the
    # last download, not the login. The session's real age is when we wrote the file.
    written_at = _written_at_timestamp(meta.get("written_at"))
    age_hours = round((datetime.now(UTC).timestamp() - (written_at or path.stat().st_mtime)) / 3600, 1)
    return {"exists": True, "path": str(path), "age_hours": age_hours,
            "stale": age_hours > STALE_AFTER_HOURS, "source": meta.get("source", "external")}


def _audio_formats(info: dict) -> list[dict]:
    audio = [
        {
            "format_id": str(f.get("format_id") or ""),
            "acodec": f.get("acodec") or "",
            "abr": f.get("abr") or 0,
            "format_note": f.get("format_note") or "",
        }
        for f in info.get("formats") or []
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    audio.sort(key=lambda f: f["abr"], reverse=True)
    return audio[:8]


def _is_unavailable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _UNAVAILABLE_MARKERS)


def _classify_error(exc: Exception, cookie: dict) -> str:
    text = str(exc)
    age = f" The cookie file is {cookie['age_hours']} h old." if cookie.get("stale") else ""
    if any(marker in text.lower() for marker in _NO_JS_RUNTIME_MARKERS):
        return NO_FORMATS_MESSAGE
    if "Sign in to confirm" in text or "not a bot" in text:
        return ("YouTube asked this session to prove it is not a bot, which means it did not accept the cookies."
                f" Paste fresh headers in the YouTube Music card.{age}")
    if "403" in text or "cookies are no longer valid" in text or "Sign in" in text:
        return f"YouTube rejected the cookies (403). They have expired — paste fresh headers to replace them.{age}"
    return f"yt-dlp could not read the track: {text[:200]}"


def _extract_info(video_id: str, cookie_path: Path) -> dict:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cookiefile": str(cookie_path),
        "remote_components": ["ejs:github"],
        "extractor_args": {"youtube": {"player_client": ["web_music"]}},
        # A zero-format answer must reach us as an empty info dict, not as a misleading
        # "Requested format is not available" — that is how a missing deno shows up.
        "ignore_no_formats_error": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False) or {}
    finally:
        note_cookie_file_rewritten()


def _probe_formats(candidates: list[str], cookie_path: Path) -> tuple[str, dict | None, Exception | None]:
    """The first probe track yt-dlp answers for; a taken-down one falls through to the next."""
    video_id, error = candidates[0], None
    for video_id in candidates:
        try:
            return video_id, _extract_info(video_id, cookie_path), None
        except Exception as e:
            error = e
            if not _is_unavailable(e):
                break
            logger.warning("Premium probe track %s is unavailable; trying the next one", video_id)
    return video_id, None, error


def premium_check(video_id: str | None = None) -> dict:
    """Ask yt-dlp for the formats of a known track and report whether Premium audio is offered.

    Blocking (network + subprocess); callers on the event loop must use asyncio.to_thread.
    """
    from services.ytdlp_opts import ensure_deno_on_path

    ensure_deno_on_path()
    candidates = [video_id or settings.premium_check_video_id]
    candidates += [i for i in PREMIUM_CHECK_FALLBACK_IDS if i not in candidates]
    cookie = cookie_file_status()
    result = {"premium": False, "formats": [], "cookie": cookie, "error": None, "state": "fail",
              "checked_at": datetime.now(UTC).isoformat(), "video_id": candidates[0]}

    if not cookie["exists"]:
        result["state"] = "not_configured"
        result["error"] = NO_COOKIE_MESSAGE.format(path=cookie["path"])
        return _cache_result(result)

    result["video_id"], info, error = _probe_formats(candidates, Path(cookie["path"]))
    if error is not None:
        result["state"] = "error"
        result["error"] = _classify_error(error, cookie)
        return _cache_result(result)

    formats = _audio_formats(info)
    if not formats:
        result["state"] = "error"
        result["error"] = NO_FORMATS_MESSAGE
        return _cache_result(result)

    result["formats"] = formats
    result["premium"] = any(
        f["format_id"] in PREMIUM_ITAGS or "premium" in f["format_note"].lower() for f in formats
    )
    result["state"] = "pass" if result["premium"] else "fail"
    if not result["premium"]:
        result["error"] = ("The cookies work but YouTube served no Premium audio, so downloads stay at about 128 kbps. "
                           "Make sure the account you copied the headers from has YouTube Music Premium.")
    return _cache_result(result)


def _cache_result(result: dict) -> dict:
    try:
        _write_atomic(_cache_path(), json.dumps(result, indent=2))
    except OSError as e:
        logger.warning("Could not cache the Premium check result: %s", e)
    return result


def cached_premium_check() -> dict | None:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def premium_check_state() -> dict:
    """The last Premium verdict with a live cookie status and the `state` the card shows:
    not_configured (no cookie file) / unchecked / pass / fail / error."""
    cached = cached_premium_check() or {"premium": None, "formats": [], "error": None,
                                        "checked_at": None, "video_id": settings.premium_check_video_id}
    cookie = cookie_file_status()
    if not cookie["exists"]:
        state = "not_configured"
    elif not cached.get("checked_at"):
        state = "unchecked"
    else:
        state = cached.get("state") or ("pass" if cached.get("premium") else "fail")
    return {**cached, "cookie": cookie, "state": state}
