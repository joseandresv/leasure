"""Cross-source music search — Spotify, YouTube Music, MusicBrainz."""

import asyncio
import logging

import httpx

from services import spotify_client as sp
from services import youtube_client as yt

logger = logging.getLogger(__name__)

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_HEADERS = {"User-Agent": "Leasure/0.2 (music downloader)", "Accept": "application/json"}


def _normalize_key(title: str, artist: str) -> str:
    return f"{title.strip().lower()}|{artist.strip().lower().split(',')[0]}"


async def search_spotify(query: str, limit: int = 20) -> dict:
    """Search Spotify catalog for albums, tracks, and artists."""
    client = sp.get_client()
    if not client:
        return {"albums": [], "tracks": [], "artists": []}

    try:
        results = client.search(q=query, type="album,track,artist", limit=limit)
    except Exception as e:
        logger.warning("Spotify search failed: %s", e)
        return {"albums": [], "tracks": [], "artists": []}

    albums = []
    for a in results.get("albums", {}).get("items", []):
        albums.append({
            "id": f"sp:{a['id']}",
            "name": a["name"],
            "artist": ", ".join(ar["name"] for ar in a.get("artists", [])),
            "image_url": a["images"][0]["url"] if a.get("images") else None,
            "year": a.get("release_date", "")[:4] if a.get("release_date") else None,
            "total_tracks": a.get("total_tracks", 0),
            "sources": [{"provider": "spotify", "id": a["id"], "uri": a.get("uri", "")}],
        })

    tracks = []
    for t in results.get("tracks", {}).get("items", []):
        tracks.append({
            "id": f"sp:{t['id']}",
            "name": t["name"],
            "artist": ", ".join(ar["name"] for ar in t.get("artists", [])),
            "album": t["album"]["name"] if t.get("album") else "",
            "image_url": t["album"]["images"][0]["url"] if t.get("album", {}).get("images") else None,
            "duration_ms": t.get("duration_ms", 0),
            "track_number": t.get("track_number", 0),
            "sources": [{"provider": "spotify", "id": t["id"], "uri": t.get("uri", ""),
                         "artist_id": t["artists"][0]["id"] if t.get("artists") else ""}],
        })

    artists = []
    for ar in results.get("artists", {}).get("items", []):
        artists.append({
            "name": ar["name"],
            "image_url": ar["images"][0]["url"] if ar.get("images") else None,
            "genres": ar.get("genres", []),
            "sources": ["spotify"],
        })

    return {"albums": albums, "tracks": tracks, "artists": artists}


async def search_youtube(query: str, limit: int = 20) -> dict:
    """Search YouTube Music catalog."""
    client = yt.get_client()
    if not client:
        return {"albums": [], "tracks": [], "artists": []}

    albums = []
    tracks = []
    artists = []

    try:
        # Search albums
        album_results = client.search(query, filter="albums", limit=limit)
        for a in album_results:
            albums.append({
                "id": f"yt:{a.get('browseId', '')}",
                "name": a.get("title", ""),
                "artist": ", ".join(ar["name"] for ar in a.get("artists", []) if ar.get("name")),
                "image_url": a["thumbnails"][-1]["url"] if a.get("thumbnails") else None,
                "year": a.get("year"),
                "total_tracks": 0,
                "sources": [{"provider": "youtube", "id": a.get("browseId", "")}],
            })
    except Exception as e:
        logger.warning("YouTube album search failed: %s", e)

    try:
        # Search songs
        song_results = client.search(query, filter="songs", limit=limit)
        for t in song_results:
            if not t.get("videoId"):
                continue
            tracks.append({
                "id": f"yt:{t['videoId']}",
                "name": t.get("title", ""),
                "artist": ", ".join(ar["name"] for ar in t.get("artists", []) if ar.get("name")),
                "album": t.get("album", {}).get("name", "") if t.get("album") else "",
                "image_url": t["thumbnails"][-1]["url"] if t.get("thumbnails") else None,
                "duration_ms": _parse_duration(t.get("duration", "")),
                "track_number": 0,
                "sources": [{"provider": "youtube", "id": t["videoId"]}],
            })
    except Exception as e:
        logger.warning("YouTube song search failed: %s", e)

    try:
        # Search artists
        artist_results = client.search(query, filter="artists", limit=limit)
        for ar in artist_results:
            artists.append({
                "name": ar.get("artist", ar.get("title", "")),
                "image_url": ar["thumbnails"][-1]["url"] if ar.get("thumbnails") else None,
                "genres": [],
                "sources": ["youtube"],
            })
    except Exception as e:
        logger.warning("YouTube artist search failed: %s", e)

    return {"albums": albums, "tracks": tracks, "artists": artists}


async def search_musicbrainz(query: str, limit: int = 10) -> dict:
    """Search MusicBrainz for album metadata enrichment."""
    albums = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{MUSICBRAINZ_API}/release",
                params={"query": query, "fmt": "json", "limit": limit},
                headers=MUSICBRAINZ_HEADERS,
            )
            if resp.status_code == 200:
                data = resp.json()
                for rel in data.get("releases", []):
                    artist = ", ".join(
                        ac.get("name", "") for ac in rel.get("artist-credit", []) if ac.get("name")
                    )
                    albums.append({
                        "id": f"mb:{rel['id']}",
                        "name": rel.get("title", ""),
                        "artist": artist,
                        "image_url": None,  # MusicBrainz doesn't serve images directly
                        "year": rel.get("date", "")[:4] if rel.get("date") else None,
                        "total_tracks": rel.get("track-count", 0),
                        "sources": [{"provider": "musicbrainz", "id": rel["id"]}],
                    })
    except Exception as e:
        logger.warning("MusicBrainz search failed: %s", e)

    return {"albums": albums, "tracks": [], "artists": []}


async def search_all(query: str, limit: int = 20) -> dict:
    """Search all available sources in parallel, merge and deduplicate."""
    sp_task = search_spotify(query, limit)
    yt_task = search_youtube(query, limit)
    mb_task = search_musicbrainz(query, min(limit, 10))

    sp_res, yt_res, mb_res = await asyncio.gather(sp_task, yt_task, mb_task, return_exceptions=True)

    # Handle exceptions
    if isinstance(sp_res, Exception):
        logger.warning("Spotify search error: %s", sp_res)
        sp_res = {"albums": [], "tracks": [], "artists": []}
    if isinstance(yt_res, Exception):
        logger.warning("YouTube search error: %s", yt_res)
        yt_res = {"albums": [], "tracks": [], "artists": []}
    if isinstance(mb_res, Exception):
        logger.warning("MusicBrainz search error: %s", mb_res)
        mb_res = {"albums": [], "tracks": [], "artists": []}

    # Merge albums
    all_albums = sp_res["albums"] + yt_res["albums"] + mb_res["albums"]
    merged_albums = _merge_items(all_albums, lambda a: _normalize_key(a["name"], a["artist"]))

    # Merge tracks
    all_tracks = sp_res["tracks"] + yt_res["tracks"]
    merged_tracks = _merge_items(all_tracks, lambda t: _normalize_key(t["name"], t["artist"]))

    # Merge artists
    all_artists = sp_res["artists"] + yt_res["artists"]
    seen_artists = {}
    for ar in all_artists:
        key = ar["name"].strip().lower()
        if key in seen_artists:
            for s in ar.get("sources", []):
                if s not in seen_artists[key]["sources"]:
                    seen_artists[key]["sources"].append(s)
        else:
            seen_artists[key] = ar
    merged_artists = list(seen_artists.values())

    return {
        "albums": merged_albums[:limit],
        "tracks": merged_tracks[:limit],
        "artists": merged_artists[:limit],
    }


def _merge_items(items: list[dict], key_fn) -> list[dict]:
    """Deduplicate items, merging sources."""
    seen = {}
    for item in items:
        key = key_fn(item)
        if key in seen:
            existing = seen[key]
            for src in item.get("sources", []):
                if src not in existing["sources"]:
                    existing["sources"].append(src)
            if item.get("image_url") and not existing.get("image_url"):
                existing["image_url"] = item["image_url"]
        else:
            seen[key] = item
    return list(seen.values())


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
