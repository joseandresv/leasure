import logging
import time

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from config import settings

logger = logging.getLogger(__name__)

SCOPES = "user-library-read playlist-read-private playlist-read-collaborative user-read-private user-read-recently-played user-read-currently-playing user-read-playback-state"
CACHE_PATH = str(settings.data_dir / ".spotify_cache")
REQUEST_TIMEOUT_S = 10

_rate_limited_until = 0.0


def _get_auth_manager() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=False,
    )


def get_auth_url(state: str | None = None) -> str:
    return _get_auth_manager().get_authorize_url(state=state)


def handle_callback(code: str) -> dict:
    auth = _get_auth_manager()
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
    return spotipy.Spotify(auth_manager=auth, requests_session=requests.Session(), requests_timeout=REQUEST_TIMEOUT_S)


def rate_limit_seconds_left() -> float:
    """Seconds until Spotify's last 429 Retry-After expires; 0 when not rate-limited."""
    return max(0.0, _rate_limited_until - time.monotonic())


def is_rate_limited() -> bool:
    return rate_limit_seconds_left() > 0


def _note_rate_limit(e: spotipy.SpotifyException) -> None:
    global _rate_limited_until
    try:
        retry_after = int((e.headers or {}).get("Retry-After") or 0)
    except ValueError:
        retry_after = 0
    if not is_rate_limited():
        logger.warning("Spotify is rate-limiting this app (429, Retry-After %ss); skipping Spotify album lookups until then",
                       retry_after)
    _rate_limited_until = time.monotonic() + retry_after


def is_connected() -> bool:
    return get_client() is not None


def get_user_profile() -> dict | None:
    sp = get_client()
    if not sp:
        return None
    return sp.current_user()


def get_artist_genres(artist_id: str) -> list[str]:
    """Fetch genres for an artist from Spotify. Returns list of genre strings."""
    sp = get_client()
    if not sp or not artist_id:
        return []
    try:
        artist = sp.artist(artist_id)
        return artist.get("genres", [])
    except Exception as e:
        logger.debug("Failed to get genres for artist %s: %s", artist_id, e)
        return []


def _isrc(t: dict) -> str | None:
    return (t.get("external_ids") or {}).get("isrc")


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
        "album_artist": ", ".join(a["name"] for a in album.get("artists", [])) or None,
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
            continue
        for t in (full or {}).get("tracks") or []:
            if t and t.get("id") and _isrc(t):
                isrcs[t["id"]] = _isrc(t)
    return isrcs


def get_saved_albums(limit: int = 20, offset: int = 0) -> dict | None:
    sp = get_client()
    if not sp:
        return None
    result = sp.current_user_saved_albums(limit=limit, offset=offset)
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


def get_playlist_tracks(playlist_id: str, limit: int = 500, offset: int = 0) -> dict | None:
    if is_rate_limited():
        return None
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
        if isinstance(e, spotipy.SpotifyException) and e.http_status == 429:
            _note_rate_limit(e)
        error_str = str(e)
        logger.error("Failed to get playlist tracks for %s: %s", playlist_id, error_str[:200])
        return {"error": error_str[:200], "playlist": playlist_info, "tracks": [], "total": 0, "offset": 0, "limit": limit}

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
        logger.debug("Failed to get currently playing: %s", e)
        return None


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
        logger.error("Failed to get recently played: %s", e)
        return None


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
