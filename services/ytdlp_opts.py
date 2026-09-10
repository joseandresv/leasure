"""Shared yt-dlp invocation for the download engines."""

import logging

from config import settings

logger = logging.getLogger(__name__)

COOKIE_FILE = settings.data_dir / "cookies.txt"


def build_download_attempts(base_opts: dict) -> list[tuple[str, dict]]:
    """Premium (cookies) then standard yt-dlp option sets, both with the PO-token solver.

    A Netscape cookies.txt in data/ wins over live browser extraction: it is cheaper
    (no keyring/decrypt on every download) and works on hosts with no browser profile
    (WSL2, headless)."""
    if COOKIE_FILE.exists():
        cookie_opts = {"cookiefile": str(COOKIE_FILE)}
    else:
        cookie_opts = {"cookiesfrombrowser": (settings.cookie_browser,)}
    return [
        ("premium", {**base_opts, **cookie_opts, "remote_components": ["ejs:github"]}),
        ("standard", {**base_opts, "remote_components": ["ejs:github"]}),
    ]


def download_with_fallback(url: str, base_opts: dict) -> None:
    """Download `url` with cookies, retrying without them if that attempt fails."""
    import yt_dlp

    for label, ydl_opts in build_download_attempts(base_opts):
        try:
            logger.info("Trying download (%s quality)", label)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return
        except Exception as e:
            if label == "premium":
                logger.warning("Premium download failed, trying without cookies: %s", str(e)[:300])
                continue
            raise
