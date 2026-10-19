import copy
import functools
import json
import logging
import math
import os
import random
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from config import settings
from services import secret_file

logger = logging.getLogger(__name__)

SCOPES = "user-library-read playlist-read-private playlist-read-collaborative user-read-private user-read-recently-played user-read-currently-playing user-read-playback-state"
FOLLOW_SCOPE = "user-follow-read"
# Asked for on (re)connect only. get_client() validates against SCOPES, so a token granted before
# FOLLOW_SCOPE existed keeps working; whether it was granted is learnt from follow_scope_status().
AUTH_SCOPES = f"{SCOPES} {FOLLOW_SCOPE}"
CACHE_PATH = str(settings.data_dir / ".spotify_cache")
RATE_LIMIT_PATH = settings.data_dir / "spotify_rate_limit.json"
CALLS_PATH = settings.data_dir / "spotify_calls.json"
REQUEST_TIMEOUT_S = 10
# A 429 without a usable Retry-After still stops calls for a while instead of letting the next one through.
MIN_RATE_LIMIT_S = 60
PAGE_CAPS = {"tracks": 60, "albums": 20, "playlists": 20, "playlist_items": 50, "artists": 20}


def _restored_rate_limit_until() -> float:
    """The monotonic deadline of a 429 window an earlier process saw, so a restart never calls into it."""
    try:
        until = float(json.loads(RATE_LIMIT_PATH.read_text(encoding="utf-8"))["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0
    left = until - time.time()
    return time.monotonic() + left if left > 0 else 0.0


_rate_limited_until = _restored_rate_limit_until()


def _get_auth_manager(scope: str = SCOPES) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        scope=scope,
        cache_path=CACHE_PATH,
        open_browser=False,
    )


def get_auth_url(state: str | None = None) -> str:
    return _get_auth_manager(AUTH_SCOPES).get_authorize_url(state=state)


def handle_callback(code: str) -> dict:
    auth = _get_auth_manager(AUTH_SCOPES)
    token_info = auth.get_access_token(code, as_dict=True)
    return token_info


def get_client() -> spotipy.Spotify | None:
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        return None
    auth = _get_auth_manager()
    try:
        token_info = auth.get_cached_token()
    except Exception as e:
        # A failed refresh (revoked app, rotated secret, offline) must not turn
        # every Spotify endpoint into a 500; report "not connected" instead so
        # the UI offers the Connect link again.
        logger.warning("Spotify token refresh failed, treating as disconnected: %s", e)
        return None
    if not token_info:
        return None
    # A plain session, not spotipy's urllib3 retry adapter: that adapter sleeps through a 429's
    # Retry-After, which Spotify sets to hours once the app is rate-limited.
    return _GuardedSpotify(auth_manager=auth, requests_session=requests.Session(), requests_timeout=REQUEST_TIMEOUT_S)


class _GuardedSpotify(spotipy.Spotify):
    """Every request goes through here: nothing is sent while rate-limited, and any 429 starts the window."""

    def _internal_call(self, method, url, payload, params):
        if is_rate_limited():
            raise spotipy.SpotifyException(429, -1, f"{url}: not sent, Spotify is rate-limiting Leasure",
                                           headers={"Retry-After": str(math.ceil(rate_limit_seconds_left()))})
        _count_call()
        try:
            return super()._internal_call(method, url, payload, params)
        except spotipy.SpotifyException as e:
            _note_rate_limit(e)
            raise


_calls_lock = threading.Lock()
_sync_call: ContextVar[bool] = ContextVar("spotify_sync_call", default=False)


@contextmanager
def counting_as_sync() -> Iterator[None]:
    """Calls sent inside this block count against the library-reading share of the day."""
    token = _sync_call.set(True)
    try:
        yield
    finally:
        _sync_call.reset(token)


def _read_calls() -> dict:
    try:
        state = json.loads(CALLS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("date") == date.today().isoformat() else {}


def calls_today() -> dict:
    """{"total", "sync"}: Web API calls sent today (local date), all of them and those of library reads."""
    state = _read_calls()
    return {"total": int(state.get("total") or 0), "sync": int(state.get("sync") or 0)}


def _count_call() -> None:
    with _calls_lock:
        counts = calls_today()
        counts["total"] += 1
        counts["sync"] += _sync_call.get()
        try:
            secret_file.write_atomic(CALLS_PATH, json.dumps({"date": date.today().isoformat(), **counts}))
        except OSError as e:
            logger.warning("Could not save the Spotify call counter to %s: %s", CALLS_PATH, e)


def rate_limit_seconds_left() -> float:
    """Seconds until Spotify's last 429 Retry-After expires; 0 when not rate-limited."""
    return max(0.0, _rate_limited_until - time.monotonic())


def is_rate_limited() -> bool:
    return rate_limit_seconds_left() > 0


def wait_text(seconds: float) -> str:
    return f"~{round(seconds / 3600)} h" if seconds >= 5400 else f"~{max(1, round(seconds / 60))} min"


def rate_limit_message() -> str | None:
    seconds = rate_limit_seconds_left()
    return f"Spotify is rate-limiting Leasure (try again in {wait_text(seconds)})" if seconds else None


def _note_rate_limit(e: Exception) -> bool:
    """Starts the rate-limit window when `e` is a 429; True if it was one."""
    global _rate_limited_until
    if not (isinstance(e, spotipy.SpotifyException) and e.http_status == 429):
        return False
    try:
        retry_after = int((e.headers or {}).get("Retry-After") or 0)
    except ValueError:
        retry_after = 0
    retry_after = max(retry_after, MIN_RATE_LIMIT_S)
    if not is_rate_limited():
        logger.warning("Spotify is rate-limiting this app (429, Retry-After %ss); skipping Spotify calls until then",
                       retry_after)
    _rate_limited_until = time.monotonic() + retry_after
    _save_rate_limit(time.time() + retry_after)
    return True


def _save_rate_limit(until: float) -> None:
    try:
        RATE_LIMIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=RATE_LIMIT_PATH.parent, prefix=f".{RATE_LIMIT_PATH.name}.", suffix=".tmp")
    except OSError as e:
        logger.warning("Could not save the Spotify rate-limit window to %s: %s", RATE_LIMIT_PATH, e)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"until": until}, fh)
        os.replace(tmp, RATE_LIMIT_PATH)
    except OSError as e:
        Path(tmp).unlink(missing_ok=True)
        logger.warning("Could not save the Spotify rate-limit window to %s: %s", RATE_LIMIT_PATH, e)


def _web_api(default=None):
    """For functions that call the Web API: `default` at once while rate-limited, and on a 429."""
    def wrap(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            if is_rate_limited():
                return copy.copy(default)
            try:
                return fn(*args, **kwargs)
            except spotipy.SpotifyException as e:
                if not _note_rate_limit(e):
                    raise
                return copy.copy(default)
        return guarded
    return wrap


def is_connected() -> bool:
    return get_client() is not None


@_web_api()
def get_user_profile() -> dict | None:
    sp = get_client()
    if not sp:
        return None
    return sp.current_user()


@_web_api(default=[])
def get_artist_genres(artist_id: str) -> list[str]:
    """Fetch genres for an artist from Spotify. Returns list of genre strings."""
    if not artist_id:
        return []
    sp = get_client()
    if not sp:
        return []
    try:
        artist = sp.artist(artist_id)
        return artist.get("genres", [])
    except Exception as e:
        _note_rate_limit(e)
        logger.debug("Failed to get genres for artist %s: %s", artist_id, e)
        return []


def _isrc(t: dict) -> str | None:
    return (t.get("external_ids") or {}).get("isrc")


def _names(artists) -> str:
    return ", ".join(a["name"] for a in artists or [] if isinstance(a, dict) and isinstance(a.get("name"), str))


def release_year(release_date: str | None) -> int | None:
    """Year from a Spotify release_date, which may be 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'."""
    if not release_date or len(release_date) < 4:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def album_fields(album: dict) -> dict:
    """The album-level credit a track carries into tagging: the album's own artists,
    not the track's, so "Earth, Wind & Fire" survives as one name."""
    return {
        "album_artist": _names(album.get("artists")) or None,
        "year": release_year(album.get("release_date")),
        "total_tracks": album.get("total_tracks"),
    }


def _fetch_isrcs(sp: spotipy.Spotify, track_ids: list[str]) -> dict[str, str]:
    """ISRC per track id. The album-tracks payload is a simplified object with no
    external_ids, so the full track objects have to be fetched (50 per request)."""
    isrcs: dict[str, str] = {}
    for start in range(0, len(track_ids), 50):
        batch = track_ids[start:start + 50]
        try:
            full = sp.tracks(batch)
        except Exception as e:
            logger.warning("Failed to fetch ISRCs for %d tracks: %s", len(batch), e)
            if _note_rate_limit(e):
                break
            continue
        for t in (full or {}).get("tracks") or []:
            if t and t.get("id") and _isrc(t):
                isrcs[t["id"]] = _isrc(t)
    return isrcs


@_web_api()
def get_saved_albums(limit: int = 20, offset: int = 0) -> dict | None:
    sp = get_client()
    if not sp:
        return None
    try:
        result = sp.current_user_saved_albums(limit=limit, offset=offset)
    except Exception as e:
        _note_rate_limit(e)
        logger.warning("Failed to get Spotify saved albums: %s", e)
        return None
    albums = []
    for item in result.get("items", []):
        album = item["album"]
        albums.append({
            "id": album["id"],
            "name": album["name"],
            "artist": ", ".join(a["name"] for a in album["artists"]),
            "image_url": album["images"][0]["url"] if album["images"] else None,
            "total_tracks": album["total_tracks"],
            "release_date": album.get("release_date", ""),
            "uri": album["uri"],
        })
    return {
        "albums": albums,
        "total": result.get("total", 0),
        "offset": result.get("offset", 0),
        "limit": result.get("limit", 20),
    }


def get_album_tracks(album_id: str, client: spotipy.Spotify | None = None) -> dict | None:
    """`client` lets a caller use a session with its own retry policy; it then also handles 429s itself.
    Without one, a rate-limited Spotify yields None at once instead of an error."""
    if client is None and is_rate_limited():
        return None
    sp = client or get_client()
    if not sp:
        return None
    try:
        album = sp.album(album_id)
    except spotipy.SpotifyException as e:
        if client is not None or e.http_status != 429:
            raise
        _note_rate_limit(e)
        return None
    isrcs = _fetch_isrcs(sp, [t["id"] for t in album["tracks"]["items"] if t.get("id")])
    credit = album_fields(album)
    tracks = []
    for t in album["tracks"]["items"]:
        tracks.append({
            "id": t["id"],
            "name": t["name"],
            "artist": ", ".join(a["name"] for a in t["artists"]),
            "artist_id": t["artists"][0]["id"] if t.get("artists") else None,
            "track_number": t["track_number"],
            "disc_number": t["disc_number"],
            "duration_ms": t["duration_ms"],
            "uri": t["uri"],
            "isrc": isrcs.get(t["id"]),
            **credit,
        })
    discs = [t["disc_number"] for t in tracks if t.get("disc_number")]
    return {
        "album": {
            "id": album["id"],
            "name": album["name"],
            "artist": ", ".join(a["name"] for a in album["artists"]),
            "image_url": album["images"][0]["url"] if album["images"] else None,
            "release_date": album.get("release_date", ""),
            "uri": album["uri"],
            "genres": album.get("genres", []),
            "album_artist": credit["album_artist"],
            "total_tracks": credit["total_tracks"] or len(tracks),
            "total_discs": max(discs) if discs else None,
        },
        "tracks": tracks,
    }


@_web_api()
def get_playlists(limit: int = 20, offset: int = 0) -> dict | None:
    sp = get_client()
    if not sp:
        return None
    result = sp.current_user_playlists(limit=limit, offset=offset)
    playlists = []
    for p in result.get("items", []):
        playlists.append({
            "id": p["id"],
            "name": p["name"],
            "owner": p["owner"]["display_name"],
            "image_url": p["images"][0]["url"] if p.get("images") else None,
            "total_tracks": (p.get("tracks") or {}).get("total", 0) or "?",
            "uri": p["uri"],
        })
    return {
        "playlists": playlists,
        "total": result.get("total", 0),
        "offset": result.get("offset", 0),
        "limit": result.get("limit", 20),
    }


@_web_api()
def get_playlist_tracks(playlist_id: str, limit: int = 500, offset: int = 0) -> dict | None:
    sp = get_client()
    if not sp:
        return None

    playlist_info = {"name": "", "image_url": None, "owner": ""}
    tracks = []
    total = 0

    try:
        # Get playlist metadata
        pl_meta = sp.playlist(playlist_id, fields="name,images,owner(display_name)")
        playlist_info = {
            "name": pl_meta.get("name", ""),
            "image_url": pl_meta["images"][0]["url"] if pl_meta.get("images") else None,
            "owner": (pl_meta.get("owner") or {}).get("display_name", ""),
        }
    except Exception as e:
        _note_rate_limit(e)
        logger.warning("Failed to get playlist metadata for %s: %s", playlist_id, e)

    try:
        # Use playlist_items — more reliable than playlist() for track data
        items_data = sp.playlist_items(playlist_id, limit=min(limit, 100), offset=offset,
                                       additional_types=("track",))
        total = items_data.get("total", 0)

        for item in items_data.get("items", []):
            # Spotify API returns track data under 'track' or 'item' key
            t = item.get("track") or item.get("item")
            if not t or item.get("is_local") or not t.get("id"):
                continue
            if t.get("type") != "track":
                continue
            tracks.append(_parse_track(t))

        # Paginate if there are more tracks
        while items_data.get("next") and len(tracks) < limit:
            items_data = sp.next(items_data)
            if not items_data:
                break
            for item in items_data.get("items", []):
                t = item.get("track") or item.get("item")
                if not t or item.get("is_local") or not t.get("id"):
                    continue
                if t.get("type") != "track":
                    continue
                tracks.append(_parse_track(t))
                if len(tracks) >= limit:
                    break

    except Exception as e:
        _note_rate_limit(e)
        error_str = str(e)
        logger.error("Failed to get playlist tracks for %s: %s", playlist_id, error_str[:200])
        result = {"error": error_str[:200], "playlist": playlist_info, "tracks": [], "total": 0, "offset": 0, "limit": limit}
        if isinstance(e, spotipy.SpotifyException):
            result["error_status"] = e.http_status
        return result

    return {
        "playlist": playlist_info,
        "tracks": tracks,
        "total": total,
        "offset": 0,
        "limit": limit,
    }


def _parse_track(t: dict) -> dict:
    """Parse a Spotify track object into our standard format."""
    return {
        "id": t["id"],
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
        "artist_id": t["artists"][0]["id"] if t.get("artists") else None,
        "album": t["album"]["name"] if t.get("album") else None,
        "album_image_url": t["album"]["images"][0]["url"] if t.get("album", {}).get("images") else None,
        "track_number": t.get("track_number"),
        "disc_number": t.get("disc_number"),
        "duration_ms": t.get("duration_ms", 0),
        "uri": t.get("uri", ""),
        "isrc": _isrc(t),
        **album_fields(t.get("album") or {}),
    }


@_web_api()
def get_currently_playing() -> dict | None:
    """Fetch the track currently playing on Spotify (if any)."""
    sp = get_client()
    if not sp:
        return None
    try:
        result = sp.current_user_playing_track()
        if not result or not result.get("is_playing"):
            return None
        t = result.get("item")
        if not t or t.get("type") != "track":
            return None
        return {
            "id": t["id"],
            "name": t["name"],
            "artist": ", ".join(a["name"] for a in t["artists"]),
            "artist_id": t["artists"][0]["id"] if t.get("artists") else None,
            "album": t["album"]["name"] if t.get("album") else None,
            "album_id": t["album"]["id"] if t.get("album") else None,
            "album_image_url": t["album"]["images"][0]["url"] if t.get("album", {}).get("images") else None,
            "track_number": t.get("track_number"),
            "disc_number": t.get("disc_number"),
            "duration_ms": t["duration_ms"],
            "progress_ms": result.get("progress_ms", 0),
            "uri": t["uri"],
            "isrc": _isrc(t),
            **album_fields(t.get("album") or {}),
        }
    except Exception as e:
        _note_rate_limit(e)
        logger.debug("Failed to get currently playing: %s", e)
        return None


@_web_api()
def get_recently_played(limit: int = 50) -> list[dict] | None:
    """Fetch recently played tracks from Spotify (last 50 max)."""
    sp = get_client()
    if not sp:
        return None
    try:
        result = sp.current_user_recently_played(limit=min(limit, 50))
        tracks = []
        seen = set()
        for item in result.get("items", []):
            t = item["track"]
            # Dedupe — Spotify returns repeat plays
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            ctx = item.get("context") or {}
            tracks.append({
                "id": t["id"],
                "name": t["name"],
                "artist": ", ".join(a["name"] for a in t["artists"]),
                "artist_id": t["artists"][0]["id"] if t.get("artists") else None,
                "album": t["album"]["name"] if t.get("album") else None,
                "album_id": t["album"]["id"] if t.get("album") else None,
                "album_image_url": t["album"]["images"][0]["url"] if t.get("album", {}).get("images") else None,
                "track_number": t.get("track_number"),
                "disc_number": t.get("disc_number"),
                "duration_ms": t["duration_ms"],
                "uri": t["uri"],
                "isrc": _isrc(t),
                **album_fields(t.get("album") or {}),
                "played_at": item.get("played_at", ""),
                "context_type": ctx.get("type"),
                "context_uri": ctx.get("uri"),
            })
        return tracks
    except Exception as e:
        _note_rate_limit(e)
        logger.error("Failed to get recently played: %s", e)
        return None


@_web_api()
def get_liked_songs(limit: int = 50, offset: int = 0) -> dict | None:
    sp = get_client()
    if not sp:
        return None
    result = sp.current_user_saved_tracks(limit=limit, offset=offset)
    tracks = []
    for item in result.get("items", []):
        t = item["track"]
        tracks.append({
            "id": t["id"],
            "name": t["name"],
            "artist": ", ".join(a["name"] for a in t["artists"]),
            "album": t["album"]["name"] if t.get("album") else None,
            "album_image_url": t["album"]["images"][0]["url"] if t.get("album", {}).get("images") else None,
            "track_number": t.get("track_number"),
            "disc_number": t.get("disc_number"),
            "duration_ms": t["duration_ms"],
            "uri": t["uri"],
            "isrc": _isrc(t),
            **album_fields(t.get("album") or {}),
        })
    return {
        "tracks": tracks,
        "total": result.get("total", 0),
        "offset": result.get("offset", 0),
        "limit": result.get("limit", 50),
    }


@_web_api()
def search_catalog(query: str, limit: int = 20) -> dict | None:
    sp = get_client()
    if not sp:
        return None
    return sp.search(q=query, type="album,track,artist", limit=limit)


class SyncPacer:
    """Spaces library reads: `gap_s` plus up to `jitter_s` between calls, and at most `burst` calls in any `window_s`."""

    def __init__(self, gap_s: float = 3.0, jitter_s: float = 1.5, burst: int = 8, window_s: float = 30.0, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[float, float], float] = random.uniform) -> None:
        self.gap_s, self.jitter_s, self.burst, self.window_s = gap_s, jitter_s, burst, window_s
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self._sent: list[float] = []

    def wait(self) -> None:
        now = self._clock()
        due = now
        if self._sent:
            due = max(due, self._sent[-1] + self.gap_s + self._jitter(0.0, self.jitter_s))
        recent = [t for t in self._sent if due - t < self.window_s]
        if len(recent) >= self.burst:
            due = max(due, recent[-self.burst] + self.window_s)
        if due > now:
            self._sleep(due - now)
        self._sent = [t for t in recent if due - t < self.window_s] + [due]


@dataclass(frozen=True)
class SavedPage:
    """One page of a library list. `next` is the offset (or, for followed artists, the cursor) of the next
    page, None when the list ended. `placeholders` are entries Spotify sent without an id (unavailable,
    episodes); they still count towards `total`."""
    items: list[dict]
    total: int | None
    raw_count: int
    placeholders: int
    next: int | str | None
    raw: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class SavedListing:
    """A whole list, or the part read before `stopped` ("rate_limited", "page_cap", "unreadable",
    "scope_missing"); `resume` is where the next read continues (None once complete)."""
    items: list[dict]
    total: int | None
    placeholders: int
    complete: bool
    resume: int | str | None
    stopped: str | None = None
    status: int | None = None


def _image(obj: dict) -> str | None:
    images = obj.get("images") if isinstance(obj.get("images"), list) else []
    url = images[0].get("url") if images and isinstance(images[0], dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _items(raw: dict) -> list:
    return raw.get("items") if isinstance(raw.get("items"), list) else []


def _offset_next(raw: dict, offset: int) -> int | None:
    count = len(_items(raw))
    total = raw.get("total") if isinstance(raw.get("total"), int) else None
    if not count or not raw.get("next") or (total is not None and offset + count >= total):
        return None
    return offset + count


def _saved_track(t: dict, *, added_at=None, local: bool = False, album: dict | None = None) -> dict:
    album = _dict(t.get("album")) or (album or {})
    artists = [a for a in t.get("artists") or [] if isinstance(a, dict)]
    return {
        "id": t.get("id"),
        "uri": t.get("uri") or "",
        "name": t.get("name") or "",
        "artist": _names(artists),
        "artist_id": artists[0].get("id") if artists else None,
        "album": album.get("name"),
        "album_id": album.get("id"),
        "album_image_url": _image(album),
        "track_number": t.get("track_number"),
        "disc_number": t.get("disc_number"),
        "duration_ms": t.get("duration_ms"),
        "isrc": _isrc(t),
        "is_local": local or bool(t.get("is_local")),
        "added_at": added_at,
        **album_fields(album),
    }


def _track_entry(item: dict) -> dict | None:
    """A liked-song or playlist entry; None for a placeholder (null, episode, or neither id nor local uri)."""
    if not isinstance(item, dict):
        return None
    # The February 2026 shape puts the track under "item"; older answers use "track".
    t = item.get("item") if isinstance(item.get("item"), dict) else item.get("track")
    if not isinstance(t, dict) or t.get("type", "track") != "track":
        return None
    local = bool(item.get("is_local") or t.get("is_local"))
    if not t.get("id") and not (local and t.get("uri")):
        return None
    return _saved_track(t, added_at=item.get("added_at"), local=local)


def _track_page(raw: dict, offset: int) -> SavedPage:
    entries = [_track_entry(item) for item in _items(raw)]
    tracks = [e for e in entries if e is not None]
    return SavedPage(items=tracks, total=raw.get("total"), raw_count=len(entries),
                     placeholders=len(entries) - len(tracks), next=_offset_next(raw, offset), raw=raw)


def _saved_album(item: dict) -> dict | None:
    album = _dict(item.get("album")) if isinstance(item, dict) else {}
    if not album.get("id"):
        return None
    embedded = _dict(album.get("tracks"))
    tracks = [_saved_track(t, album=album) for t in _items(embedded) if isinstance(t, dict) and t.get("id")]
    artists = [a for a in album.get("artists") or [] if isinstance(a, dict)]
    return {
        "id": album["id"],
        "uri": album.get("uri") or "",
        "name": album.get("name") or "",
        "artist": _names(artists),
        "artist_id": artists[0].get("id") if artists else None,
        "image_url": _image(album),
        "release_date": album.get("release_date") or "",
        "added_at": item.get("added_at"),
        "tracks": tracks if "items" in embedded else None,
        "tracks_total": embedded.get("total"),
        **album_fields(album),
    }


def _saved_playlist(p: dict) -> dict | None:
    if not isinstance(p, dict) or not p.get("id"):
        return None
    owner = _dict(p.get("owner"))
    # Older answers count under "tracks", February 2026 ones under "items".
    count = _dict(p.get("items")).get("total", _dict(p.get("tracks")).get("total"))
    return {
        "id": p["id"],
        "uri": p.get("uri") or "",
        "name": p.get("name") or "",
        "owner_id": owner.get("id"),
        "owner_name": owner.get("display_name"),
        "collaborative": bool(p.get("collaborative")),
        "public": p.get("public"),
        "snapshot_id": p.get("snapshot_id"),
        "track_count": count if isinstance(count, int) else None,
        "image_url": _image(p),
    }


def _followed_artist(a: dict) -> dict | None:
    if not isinstance(a, dict) or not a.get("id"):
        return None
    return {"id": a["id"], "uri": a.get("uri") or "", "name": a.get("name") or "", "image_url": _image(a),
            "genres": [g for g in a.get("genres") or [] if isinstance(g, str)]}


def _parsed_page(raw: dict, offset: int, parse) -> SavedPage:
    parsed = [parse(item) for item in _items(raw)]
    items = [p for p in parsed if p is not None]
    return SavedPage(items=items, total=raw.get("total"), raw_count=len(parsed),
                     placeholders=len(parsed) - len(items), next=_offset_next(raw, offset), raw=raw)


@_web_api()
def saved_tracks_page(offset: int = 0, limit: int = 50, client: spotipy.Spotify | None = None) -> SavedPage | None:
    """Liked songs, newest first. None when not connected or rate-limited."""
    sp = client or get_client()
    if not sp:
        return None
    return _track_page(_dict(sp.current_user_saved_tracks(limit=limit, offset=offset)), offset)


@_web_api()
def saved_albums_page(offset: int = 0, limit: int = 50, client: spotipy.Spotify | None = None) -> SavedPage | None:
    sp = client or get_client()
    if not sp:
        return None
    return _parsed_page(_dict(sp.current_user_saved_albums(limit=limit, offset=offset)), offset, _saved_album)


@_web_api()
def user_playlists_page(offset: int = 0, limit: int = 50, client: spotipy.Spotify | None = None) -> SavedPage | None:
    """Owned, collaborative and followed playlists; compare `owner_id` with the user's id to tell them apart."""
    sp = client or get_client()
    if not sp:
        return None
    return _parsed_page(_dict(sp.current_user_playlists(limit=limit, offset=offset)), offset, _saved_playlist)


@_web_api()
def playlist_items_page(playlist_id: str, offset: int = 0, limit: int = 100,
                        client: spotipy.Spotify | None = None) -> SavedPage | None:
    """No `fields=` filter: with tracks under "item", a track(...) expression answers 200 with empty items."""
    sp = client or get_client()
    if not sp:
        return None
    raw = sp.playlist_items(playlist_id, limit=limit, offset=offset, additional_types=("track",))
    return _track_page(_dict(raw), offset)


@_web_api()
def followed_artists_page(after: str | None = None, limit: int = 50,
                          client: spotipy.Spotify | None = None) -> SavedPage | None:
    """Needs FOLLOW_SCOPE. Cursor-paged: `next` is the `after` value for the following page."""
    sp = client or get_client()
    if not sp:
        return None
    raw = _dict(_dict(sp.current_user_followed_artists(limit=limit, after=after)).get("artists"))
    parsed = [_followed_artist(a) for a in _items(raw)]
    items = [a for a in parsed if a is not None]
    cursor = _dict(raw.get("cursors")).get("after")
    more = bool(parsed) and bool(raw.get("next")) and isinstance(cursor, str) and cursor != after
    return SavedPage(items=items, total=raw.get("total"), raw_count=len(parsed),
                     placeholders=len(parsed) - len(items), next=cursor if more else None, raw=raw)


def _read_all(fetch: Callable[[int | str | None], SavedPage | None], start: int | str | None, page_cap: int,
              pace: Callable[[], None] | None) -> SavedListing | None:
    items: list[dict] = []
    total, placeholders, position = None, 0, start
    for _ in range(page_cap):
        if pace is not None:
            pace()
        page = fetch(position)
        if page is None:
            if not is_rate_limited() and position == start:
                return None
            return SavedListing(items, total, placeholders, complete=False, resume=position,
                                stopped="rate_limited" if is_rate_limited() else "not_connected")
        items += page.items
        placeholders += page.placeholders
        total = page.total if isinstance(page.total, int) else total
        if page.next is None:
            return SavedListing(items, total, placeholders, complete=True, resume=None)
        position = page.next
    return SavedListing(items, total, placeholders, complete=False, resume=position, stopped="page_cap")


def _refused(e: spotipy.SpotifyException, stopped: str, start) -> SavedListing:
    return SavedListing([], None, 0, complete=False, resume=start, stopped=stopped, status=e.http_status)


@_web_api()
def fetch_saved_tracks(start: int = 0, page_cap: int = PAGE_CAPS["tracks"], pace: Callable[[], None] | None = None,
                       client: spotipy.Spotify | None = None) -> SavedListing | None:
    """Every liked song from `start`. None when not connected or rate-limited before the first page."""
    sp = client or get_client()
    if not sp:
        return None
    return _read_all(lambda offset: saved_tracks_page(offset, 50, client=sp), start, page_cap, pace)


@_web_api()
def fetch_saved_albums(start: int = 0, page_cap: int = PAGE_CAPS["albums"], pace: Callable[[], None] | None = None,
                       client: spotipy.Spotify | None = None) -> SavedListing | None:
    sp = client or get_client()
    if not sp:
        return None
    return _read_all(lambda offset: saved_albums_page(offset, 50, client=sp), start, page_cap, pace)


@_web_api()
def fetch_user_playlists(start: int = 0, page_cap: int = PAGE_CAPS["playlists"], pace: Callable[[], None] | None = None,
                         client: spotipy.Spotify | None = None) -> SavedListing | None:
    sp = client or get_client()
    if not sp:
        return None
    return _read_all(lambda offset: user_playlists_page(offset, 50, client=sp), start, page_cap, pace)


@_web_api()
def fetch_playlist_items(playlist_id: str, start: int = 0, page_cap: int = PAGE_CAPS["playlist_items"],
                         pace: Callable[[], None] | None = None,
                         client: spotipy.Spotify | None = None) -> SavedListing | None:
    """A 403 or 404 (a playlist Spotify will not show this app) ends as stopped="unreadable"."""
    sp = client or get_client()
    if not sp:
        return None
    try:
        return _read_all(lambda offset: playlist_items_page(playlist_id, offset, 100, client=sp), start, page_cap, pace)
    except spotipy.SpotifyException as e:
        if e.http_status not in (403, 404):
            raise
        return _refused(e, "unreadable", start)


@_web_api()
def fetch_followed_artists(after: str | None = None, page_cap: int = PAGE_CAPS["artists"],
                           pace: Callable[[], None] | None = None,
                           client: spotipy.Spotify | None = None) -> SavedListing | None:
    sp = client or get_client()
    if not sp:
        return None
    try:
        return _read_all(lambda cursor: followed_artists_page(cursor, 50, client=sp), after, page_cap, pace)
    except spotipy.SpotifyException as e:
        if e.http_status != 403:
            raise
        return _refused(e, _follow_refusal(e), after)


def _follow_refusal(e: spotipy.SpotifyException) -> str:
    return "scope_missing" if "scope" in str(e.msg).lower() else "refused"


@_web_api()
def follow_scope_status(client: spotipy.Spotify | None = None) -> str | None:
    """"granted" | "scope_missing" | "refused" from one GET /me/following?type=artist&limit=1.
    The token cache cannot answer this: spotipy rewrites its `scope` to the requested one on every refresh."""
    sp = client or get_client()
    if not sp:
        return None
    try:
        sp.current_user_followed_artists(limit=1)
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            raise
        logger.warning("Spotify follow-scope probe answered HTTP %s", e.http_status)
        return _follow_refusal(e) if e.http_status == 403 else "refused"
    return "granted"


@_web_api()
def saved_contains(kind: str, ids: list[str], client: spotipy.Spotify | None = None) -> list[bool] | None:
    """Whether each of `ids` (at most 50) is still a liked song ("track") or saved album ("album").
    None when Spotify does not answer it for this app (it may not in development mode)."""
    sp = client or get_client()
    if not sp:
        return None
    try:
        answer = (sp.current_user_saved_tracks_contains(tracks=ids) if kind == "track"
                  else sp.current_user_saved_albums_contains(albums=ids))
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            raise
        logger.info("Spotify saved-contains answered HTTP %s", e.http_status)
        return None
    if not isinstance(answer, list) or len(answer) != len(ids):
        return None
    return [value is True for value in answer]
