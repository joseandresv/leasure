"""The YouTube cookie file yt-dlp downloads with, and the Premium gate that proves it works.

Premium quality (itag 141 AAC 256 / 774 Opus) is only served to a logged-in Premium
session on a music.youtube.com URL. A browser profile is not readable on every host
(WSL2 has none; Chrome 127+ app-bound encryption blocks extraction on Windows), so the
Netscape cookies.txt written here — derived from the manual header paste — is the
primary credential, and `premium_check()` makes "does Premium reach yt-dlp?" a fact.
"""

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

NO_COOKIE_MESSAGE = (
    "No cookie file yet: paste your browser headers in the YouTube Music card, "
    "or export cookies.txt from a browser logged into music.youtube.com and save it as {path}."
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


def _netscape_lines(cookie_header: str) -> list[str]:
    expiry = int((datetime.now(UTC) + timedelta(days=COOKIE_LIFETIME_DAYS)).timestamp())
    lines = []
    for pair in cookie_header.split("; "):
        name, sep, value = pair.strip().partition("=")
        if not sep or not is_auth_cookie(name):
            continue
        domains = [".youtube.com"]
        if name in _GOOGLE_DOMAIN_NAMES or name.startswith(AUTH_COOKIE_PREFIX):
            domains.append(".google.com")
        for domain in domains:
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
    return lines


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
    lines = _netscape_lines(cookie_header) if cookie_header else []
    if not lines:
        logger.warning("No usable YouTube auth cookies in %s", headers_path)
        return None

    path = cookie_file_path()
    _write_atomic(path, "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n")
    _write_atomic(_meta_path(), json.dumps({"source": "headers", "written_at": datetime.now(UTC).isoformat()}))
    logger.info("Wrote %d YouTube cookie lines to %s", len(lines), path)
    return path


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
        _write_atomic(_meta_path(), json.dumps({"source": "browser", "written_at": datetime.now(UTC).isoformat()}))
        logger.info("Refreshed cookie file from %s", settings.cookie_browser)
        return True
    except Exception as e:
        logger.warning("Cookie refresh from %s failed: %s", settings.cookie_browser, str(e)[:300])
        return False


def cookie_file_status() -> dict:
    """Whether a cookie file exists, how old it is, and where it came from."""
    path = cookie_file_path()
    if not path.exists():
        return {"exists": False, "path": str(path), "age_hours": None, "stale": True, "source": None}

    age_hours = round((datetime.now(UTC).timestamp() - path.stat().st_mtime) / 3600, 1)
    source = "external"
    try:
        source = json.loads(_meta_path().read_text(encoding="utf-8")).get("source", "external")
    except (OSError, ValueError):
        pass
    return {"exists": True, "path": str(path), "age_hours": age_hours,
            "stale": age_hours > STALE_AFTER_HOURS, "source": source}


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


def _classify_error(exc: Exception, cookie: dict) -> str:
    text = str(exc)
    age = f" The cookie file is {cookie['age_hours']} h old." if cookie.get("stale") else ""
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
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False) or {}


def premium_check(video_id: str | None = None) -> dict:
    """Ask yt-dlp for the formats of a known track and report whether Premium audio is offered.

    Blocking (network + subprocess); callers on the event loop must use asyncio.to_thread.
    """
    video_id = video_id or settings.premium_check_video_id
    cookie = cookie_file_status()
    result = {"premium": False, "formats": [], "cookie": cookie, "error": None, "state": "fail",
              "checked_at": datetime.now(UTC).isoformat(), "video_id": video_id}

    if not cookie["exists"]:
        result["state"] = "not_configured"
        result["error"] = NO_COOKIE_MESSAGE.format(path=cookie["path"])
        return _cache_result(result)

    try:
        info = _extract_info(video_id, Path(cookie["path"]))
    except Exception as e:
        result["state"] = "error"
        result["error"] = _classify_error(e, cookie)
        return _cache_result(result)

    formats = _audio_formats(info)
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
