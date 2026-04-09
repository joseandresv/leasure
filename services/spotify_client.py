import logging
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from config import settings

logger = logging.getLogger(__name__)

SCOPES = "user-library-read playlist-read-private playlist-read-collaborative user-read-private"
CACHE_PATH = str(settings.data_dir / ".spotify_cache")


def _get_auth_manager() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=False,
    )


def get_auth_url() -> str:
    return _get_auth_manager().get_authorize_url()


def handle_callback(code: str) -> dict:
    auth = _get_auth_manager()
    token_info = auth.get_access_token(code, as_dict=True)
    return token_info


def get_client() -> spotipy.Spotify | None:
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        return None
    auth = _get_auth_manager()
    token_info = auth.get_cached_token()
    if not token_info:
        return None
    return spotipy.Spotify(auth_manager=auth)


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


def get_album_tracks(album_id: str) -> dict | None:
    sp = get_client()
    if not sp:
        return None
    album = sp.album(album_id)
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
        })
    return {
        "album": {
            "id": album["id"],
            "name": album["name"],
            "artist": ", ".join(a["name"] for a in album["artists"]),
            "image_url": album["images"][0]["url"] if album["images"] else None,
            "release_date": album.get("release_date", ""),
            "uri": album["uri"],
            "genres": album.get("genres", []),
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
    }


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
            "duration_ms": t["duration_ms"],
            "uri": t["uri"],
        })
    return {
        "tracks": tracks,
        "total": result.get("total", 0),
        "offset": result.get("offset", 0),
        "limit": result.get("limit", 50),
    }
