import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)

HEADERS_PATH = settings.data_dir / "youtube_headers.json"
OAUTH_TOKEN_PATH = settings.data_dir / "youtube_oauth.json"

# Google OAuth2 endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"


def get_client():
    try:
        from ytmusicapi import YTMusic

        if HEADERS_PATH.exists():
            return YTMusic(str(HEADERS_PATH))
        # Unauthenticated client (can search but not access library)
        return None
    except Exception as e:
        logger.warning("Failed to create YouTube Music client: %s", e)
        return None


def is_connected() -> bool:
    yt = get_client()
    if not yt:
        # Try auto-refresh from Chrome
        if _refresh_from_chrome():
            yt = get_client()
        if not yt:
            return False
    try:
        result = yt.get_library_playlists(limit=1)
        if len(result) > 0:
            return True
        # Auth might be stale, try refresh
        if _refresh_from_chrome():
            yt = get_client()
            if yt:
                result = yt.get_library_playlists(limit=1)
                return len(result) > 0
        return False
    except Exception:
        return False


def _refresh_from_chrome() -> bool:
    """Try to auto-extract YouTube Music cookies from Chrome browser."""
    try:
        import yt_dlp
        # Use yt-dlp's cookie extraction to get fresh cookies from Chrome
        ydl_opts = {"quiet": True, "cookiesfrombrowser": ("chrome",)}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            cookie_jar = ydl.cookiejar
            # Build cookie string
            cookies = []
            for c in cookie_jar:
                if ".youtube.com" in c.domain:
                    cookies.append(f"{c.name}={c.value}")

            if not cookies:
                return False

            cookie_str = "; ".join(cookies)

            # Find SAPISIDHASH-relevant cookies
            sapisid = None
            for c in cookie_jar:
                if c.name == "SAPISID":
                    sapisid = c.value
                    break

            if not sapisid:
                return False

            # Generate SAPISIDHASH
            import hashlib
            import time
            timestamp = int(time.time())
            hash_input = f"{timestamp} {sapisid} https://music.youtube.com"
            hash_value = hashlib.sha1(hash_input.encode()).hexdigest()
            auth = f"SAPISIDHASH {timestamp}_{hash_value}_u"

            headers_raw = f"""cookie: {cookie_str}
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36
accept: */*
accept-language: en-US,en;q=0.9
origin: https://music.youtube.com
authorization: {auth}
x-youtube-client-name: 67
x-youtube-client-version: 1.20260403.09.00
x-goog-authuser: 0
x-origin: https://music.youtube.com"""

            from ytmusicapi import setup
            setup(filepath=str(HEADERS_PATH), headers_raw=headers_raw)
            logger.info("Auto-refreshed YouTube Music auth from Chrome cookies")
            return True
    except Exception as e:
        logger.debug("Chrome cookie auto-refresh failed: %s", e)
        return False


def setup_from_headers(raw_headers: str) -> bool:
    """Set up YouTube Music auth from raw browser headers or cURL command."""
    try:
        # If it looks like a cURL command, extract headers from it
        if raw_headers.strip().startswith("curl"):
            raw_headers = _extract_headers_from_curl(raw_headers)

        from ytmusicapi import setup
        result = setup(filepath=str(HEADERS_PATH), headers_raw=raw_headers)
        logger.info("YouTube Music auth configured")
        return True
    except Exception as e:
        logger.error("Failed to setup YouTube Music auth: %s", e)
        return False


def _extract_headers_from_curl(curl_cmd: str) -> str:
    """Convert a cURL command to raw headers format."""
    import re
    headers = []
    # Match -H 'Header: Value' or -H "Header: Value"
    for match in re.finditer(r"""-H\s+['"](.*?)['"]""", curl_cmd):
        headers.append(match.group(1))
    return "\n".join(headers)


def get_playlists() -> list[dict] | None:
    yt = get_client()
    if not yt:
        return None
    try:
        result = yt.get_library_playlists(limit=50)
        return [
            {
                "id": p["playlistId"],
                "name": p["title"],
                "count": p.get("count", "?"),
                "image_url": p["thumbnails"][-1]["url"] if p.get("thumbnails") else None,
            }
            for p in result
        ]
    except Exception as e:
        logger.error("Failed to get YT playlists: %s", e)
        return None


def get_playlist_tracks(playlist_id: str) -> dict | None:
    yt = get_client()
    if not yt:
        return None
    try:
        playlist = yt.get_playlist(playlist_id, limit=500)
        tracks = []
        for t in playlist.get("tracks", []):
            if not t.get("videoId"):
                continue
            tracks.append({
                "id": t["videoId"],
                "name": t["title"],
                "artist": ", ".join(a["name"] for a in t.get("artists", []) if a.get("name")),
                "album": t.get("album", {}).get("name") if t.get("album") else None,
                "duration_ms": _parse_duration(t.get("duration", "")),
                "image_url": t["thumbnails"][-1]["url"] if t.get("thumbnails") else None,
            })
        return {
            "playlist": {
                "name": playlist.get("title", ""),
                "description": playlist.get("description", ""),
                "image_url": playlist["thumbnails"][-1]["url"] if playlist.get("thumbnails") else None,
            },
            "tracks": tracks,
        }
    except Exception as e:
        logger.error("Failed to get YT playlist tracks: %s", e)
        return None


def get_history(limit: int = 50) -> list[dict] | None:
    """Fetch recently played tracks from YouTube Music history."""
    yt = get_client()
    if not yt:
        return None
    try:
        result = yt.get_history()
        tracks = []
        seen = set()
        for t in result:
            if not t.get("videoId"):
                continue
            # Dedupe repeat plays
            if t["videoId"] in seen:
                continue
            seen.add(t["videoId"])
            tracks.append({
                "id": t["videoId"],
                "name": t["title"],
                "artist": ", ".join(a["name"] for a in t.get("artists", []) if a.get("name")),
                "album": t.get("album", {}).get("name") if t.get("album") else None,
                "duration_ms": _parse_duration(t.get("duration", "")),
                "image_url": t["thumbnails"][-1]["url"] if t.get("thumbnails") else None,
                "played": t.get("played", ""),  # Coarse bucket: "Today", "Yesterday", or date
            })
            if len(tracks) >= limit:
                break
        return tracks
    except Exception as e:
        logger.error("Failed to get YT history: %s", e)
        return None


# --- Google OAuth2 for YouTube history ---

def get_youtube_oauth_url() -> str | None:
    """Generate Google OAuth2 authorization URL."""
    if not settings.google_client_id:
        return None
    from urllib.parse import urlencode
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def handle_youtube_oauth_callback(code: str) -> bool:
    """Exchange auth code for tokens and store them."""
    try:
        resp = httpx.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        resp.raise_for_status()
        token_data = resp.json()
        token_data["obtained_at"] = int(time.time())
        OAUTH_TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        logger.info("YouTube OAuth tokens saved")
        return True
    except Exception as e:
        logger.error("YouTube OAuth token exchange failed: %s", e)
        return False


def _get_youtube_access_token() -> str | None:
    """Get a valid YouTube OAuth access token, refreshing if expired."""
    if not OAUTH_TOKEN_PATH.exists():
        return None
    try:
        token_data = json.loads(OAUTH_TOKEN_PATH.read_text())
    except Exception:
        return None

    # Check if token is expired (with 60s buffer)
    obtained_at = token_data.get("obtained_at", 0)
    expires_in = token_data.get("expires_in", 3600)
    if time.time() > obtained_at + expires_in - 60:
        # Refresh the token
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            logger.warning("YouTube OAuth token expired and no refresh token available")
            return None
        try:
            resp = httpx.post(GOOGLE_TOKEN_URL, data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            }, timeout=15)
            resp.raise_for_status()
            new_data = resp.json()
            # Preserve refresh_token (Google doesn't always return it on refresh)
            new_data["refresh_token"] = refresh_token
            new_data["obtained_at"] = int(time.time())
            OAUTH_TOKEN_PATH.write_text(json.dumps(new_data, indent=2))
            logger.info("YouTube OAuth token refreshed")
            return new_data["access_token"]
        except Exception as e:
            logger.error("YouTube OAuth token refresh failed: %s", e)
            return None

    return token_data.get("access_token")


def is_youtube_oauth_connected() -> bool:
    """Check if YouTube OAuth is set up and has valid tokens."""
    return _get_youtube_access_token() is not None


def get_youtube_history(limit: int = 50) -> list[dict] | None:
    """Fetch watch history from plain YouTube via InnerTube browse API with OAuth."""
    access_token = _get_youtube_access_token()
    if not access_token:
        return None

    # Use TVHTML5 InnerTube client — accepts OAuth Bearer tokens
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (PlayStation 4 5.55) AppleWebKit/601.2 (KHTML, like Gecko)",
        "x-youtube-client-name": "7",
        "x-youtube-client-version": "7.20260403.09.00",
    }

    payload = {
        "context": {
            "client": {
                "clientName": "TVHTML5",
                "clientVersion": "7.20260403.09.00",
                "hl": "en",
                "gl": "US",
            }
        },
        "browseId": "FEhistory",
    }

    try:
        resp = httpx.post(
            "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        tracks = []
        seen = set()

        # Navigate the TVHTML5 InnerTube response structure
        grid = (
            data.get("contents", {})
            .get("tvBrowseRenderer", {})
            .get("content", {})
            .get("tvSurfaceContentRenderer", {})
            .get("content", {})
            .get("gridRenderer", {})
        )
        items = grid.get("items", [])

        for item in items:
            tile = item.get("tileRenderer", {})
            if not tile:
                continue

            # Extract videoId from onSelectCommand (may be nested under commandExecutorCommand)
            cmd = tile.get("onSelectCommand", {})
            video_id = cmd.get("watchEndpoint", {}).get("videoId", "")
            if not video_id:
                nested = cmd.get("commandExecutorCommand", {}).get("commands", [])
                for nc in nested:
                    video_id = nc.get("watchEndpoint", {}).get("videoId", "")
                    if video_id:
                        break
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)

            metadata = tile.get("metadata", {}).get("tileMetadataRenderer", {})
            title = metadata.get("title", {}).get("simpleText", "")

            # Artist is the first line's first text run
            artist = ""
            for line in metadata.get("lines", []):
                line_items = line.get("lineRenderer", {}).get("items", [])
                for li in line_items:
                    text_runs = li.get("lineItemRenderer", {}).get("text", {}).get("runs", [])
                    if text_runs:
                        artist = text_runs[0].get("text", "")
                        break
                if artist:
                    break

            # Duration lives in a thumbnailOverlayTimeStatusRenderer on the header
            duration_text = ""
            header = tile.get("header", {}).get("tileHeaderRenderer", {})
            for overlay in header.get("thumbnailOverlays", []):
                ts = overlay.get("thumbnailOverlayTimeStatusRenderer", {})
                if ts:
                    duration_text = ts.get("text", {}).get("simpleText", "")
                    break

            thumbnails = header.get("thumbnail", {}).get("thumbnails", [])

            tracks.append({
                "id": video_id,
                "name": title,
                "artist": artist,
                "album": None,
                "duration_ms": _parse_duration(duration_text),
                "image_url": thumbnails[-1]["url"] if thumbnails else None,
            })
            if len(tracks) >= limit:
                break

        logger.info("Fetched %d tracks from YouTube watch history", len(tracks))
        return tracks if tracks else None
    except Exception as e:
        logger.error("Failed to get YouTube history: %s", e)
        return None


def get_liked_songs(limit: int = 100) -> list[dict] | None:
    yt = get_client()
    if not yt:
        return None
    try:
        result = yt.get_liked_songs(limit=limit)
        tracks = []
        for t in result.get("tracks", []):
            if not t.get("videoId"):
                continue
            tracks.append({
                "id": t["videoId"],
                "name": t["title"],
                "artist": ", ".join(a["name"] for a in t.get("artists", []) if a.get("name")),
                "album": t.get("album", {}).get("name") if t.get("album") else None,
                "duration_ms": _parse_duration(t.get("duration", "")),
                "image_url": t["thumbnails"][-1]["url"] if t.get("thumbnails") else None,
            })
        return tracks
    except Exception as e:
        logger.error("Failed to get YT liked songs: %s", e)
        return None


def get_library_albums(limit: int = 50) -> list[dict] | None:
    yt = get_client()
    if not yt:
        return None
    try:
        result = yt.get_library_albums(limit=limit)
        return [
            {
                "id": a["browseId"],
                "name": a["title"],
                "artist": ", ".join(ar["name"] for ar in a.get("artists", []) if ar.get("name")),
                "image_url": a["thumbnails"][-1]["url"] if a.get("thumbnails") else None,
                "year": a.get("year"),
            }
            for a in result
        ]
    except Exception as e:
        logger.error("Failed to get YT library albums: %s", e)
        return None


def get_album_tracks(browse_id: str) -> dict | None:
    yt = get_client()
    if not yt:
        return None
    try:
        album = yt.get_album(browse_id)
        tracks = []
        for t in album.get("tracks", []):
            tracks.append({
                "id": t.get("videoId"),
                "name": t["title"],
                "artist": ", ".join(a["name"] for a in t.get("artists", []) if a.get("name")),
                "track_number": t.get("index"),
                "duration_ms": _parse_duration(t.get("duration", "")),
            })
        return {
            "album": {
                "name": album.get("title", ""),
                "artist": ", ".join(a["name"] for a in album.get("artists", []) if a.get("name")),
                "image_url": album["thumbnails"][-1]["url"] if album.get("thumbnails") else None,
                "year": album.get("year"),
            },
            "tracks": tracks,
        }
    except Exception as e:
        logger.error("Failed to get YT album tracks: %s", e)
        return None


def _parse_duration(duration_str: str) -> int:
    if not duration_str:
        return 0
    parts = duration_str.split(":")
    try:
        if len(parts) == 3:
            return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
        elif len(parts) == 2:
            return (int(parts[0]) * 60 + int(parts[1])) * 1000
        return int(parts[0]) * 1000
    except ValueError:
        return 0
