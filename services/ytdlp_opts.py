"""Shared yt-dlp invocation for the download engine."""

import logging

from config import settings
from services import cookies

logger = logging.getLogger(__name__)

# The cookies are the owner's Google session, so authenticated attempts crawl:
# jittered sleeps, a few retries, and a bandwidth ceiling instead of a burst.
_HYGIENE = {
    "sleep_interval": 10,
    "max_sleep_interval": 30,
    "sleep_interval_requests": 1,
    "retries": 3,
    "fragment_retries": 3,
    "ratelimit": 5_000_000,
}
_SESSION_REFUSED = ("sign in to confirm", "http error 403", "403: forbidden", "cookies are no longer valid")


class CookieSessionRefused(RuntimeError):
    """YouTube rejected an authenticated attempt — retrying cookie-less only hides it."""


def build_download_attempts(base_opts: dict) -> list[tuple[str, dict]]:
    """yt-dlp option sets to try in order, most authenticated first.

    The cookie file (services.cookies) wins over live browser extraction: it is cheaper
    (no keyring/decrypt on every download) and works on hosts with no browser profile
    (WSL2, headless). When it exists there is no cookie-less retry — a refusal means the
    cookies need refreshing, and an anonymous retry would only serve 128 kbps."""
    common = {**base_opts, "remote_components": ["ejs:github"]}
    status = cookies.cookie_file_status()
    if status["exists"]:
        if status["stale"]:
            logger.warning("Cookie file is %s h old — refresh it if YouTube starts refusing", status["age_hours"])
        return [("cookies.txt", {**common, **_HYGIENE, "cookiefile": str(cookies.cookie_file_path())})]
    return [
        ("browser cookies", {**common, **_HYGIENE, "cookiesfrombrowser": (settings.cookie_browser,)}),
        ("no cookies", common),
    ]


def _authenticated(ydl_opts: dict) -> bool:
    return bool(ydl_opts.get("cookiefile") or ydl_opts.get("cookiesfrombrowser"))


def _extract(url: str, ydl_opts: dict, download: bool, label: str) -> dict:
    import yt_dlp

    logger.info("yt-dlp attempt: %s", label)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=download) or {}
    except CookieSessionRefused:
        raise
    except Exception as e:
        message = str(e).lower()
        if _authenticated(ydl_opts) and any(marker in message for marker in _SESSION_REFUSED):
            raise CookieSessionRefused("YouTube refused the logged-in session; refresh cookies") from e
        raise


def run_with_attempts(url: str, base_opts: dict, download: bool = True) -> dict:
    """extract_info over the attempt list, returning the first successful info dict."""
    *fallbacks, final = build_download_attempts(base_opts)
    for label, ydl_opts in fallbacks:
        try:
            return _extract(url, ydl_opts, download, label)
        except CookieSessionRefused:
            raise
        except Exception as e:
            logger.warning("yt-dlp attempt %s failed: %s", label, str(e)[:300])
    return _extract(url, final[1], download, final[0])
