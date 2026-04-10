"""Unified music aggregator — merges Spotify + YouTube Music libraries."""

import asyncio
import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Track
from services import spotify_client as sp
from services import youtube_client as yt

logger = logging.getLogger(__name__)


def _normalize_key(title: str, artist: str) -> str:
    """Create a dedup key from title + artist."""
    return f"{title.strip().lower()}|{artist.strip().lower().split(',')[0]}"


def _merge_sources(items: list[dict], key_fn) -> list[dict]:
    """Deduplicate items by key, merging their sources lists."""
    seen = {}
    for item in items:
        key = key_fn(item)
        if key in seen:
            existing = seen[key]
            for src in item.get("sources", []):
                if src not in existing["sources"]:
                    existing["sources"].append(src)
            # Prefer higher-quality image
            if item.get("image_url") and not existing.get("image_url"):
                existing["image_url"] = item["image_url"]
        else:
            seen[key] = item
    return list(seen.values())


async def enrich_with_download_status(tracks: list[dict], session: AsyncSession):
    """Check Track DB for existing downloads and set download_status on each track."""
    for track in tracks:
        status = None
        for src in track.get("sources", []):
            if src["provider"] == "spotify" and src.get("uri"):
                stmt = select(Track).where(Track.spotify_uri == src["uri"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()
                if existing:
                    status = existing.status
                    break
            elif src["provider"] == "youtube" and src.get("id"):
                stmt = select(Track).where(Track.youtube_id == src["id"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()
                if existing:
                    status = existing.status
                    break
        track["download_status"] = status


def get_unified_albums() -> list[dict]:
    """Merge Spotify saved albums + YouTube library albums."""
    albums = []

    # Spotify albums
    sp_data = sp.get_saved_albums(limit=50, offset=0)
    if sp_data:
        for a in sp_data.get("albums", []):
            albums.append({
                "id": f"sp:{a['id']}",
                "name": a["name"],
                "artist": a["artist"],
                "image_url": a.get("image_url"),
                "year": a.get("release_date", "")[:4] if a.get("release_date") else None,
                "total_tracks": a.get("total_tracks", 0),
                "sources": [{"provider": "spotify", "id": a["id"], "uri": a.get("uri", "")}],
            })

    # YouTube albums
    yt_albums = yt.get_library_albums(limit=50)
    if yt_albums:
        for a in yt_albums:
            albums.append({
                "id": f"yt:{a['id']}",
                "name": a["name"],
                "artist": a["artist"],
                "image_url": a.get("image_url"),
                "year": a.get("year"),
                "total_tracks": 0,
                "sources": [{"provider": "youtube", "id": a["id"]}],
            })

    return _merge_sources(albums, lambda a: _normalize_key(a["name"], a["artist"]))


def get_unified_recent(limit: int = 50) -> list[dict]:
    """Merge Spotify liked songs + YouTube liked songs (most recent)."""
    tracks = []

    # Spotify liked
    sp_data = sp.get_liked_songs(limit=limit, offset=0)
    if sp_data:
        for t in sp_data.get("tracks", []):
            tracks.append({
                "id": f"sp:{t['id']}",
                "name": t["name"],
                "artist": t["artist"],
                "album": t.get("album", ""),
                "image_url": t.get("album_image_url"),
                "duration_ms": t.get("duration_ms", 0),
                "track_number": t.get("track_number", 0),
                "sources": [{"provider": "spotify", "id": t["id"], "uri": t.get("uri", ""),
                             "artist_id": t.get("artist_id", "")}],
            })

    # YouTube liked
    yt_tracks = yt.get_liked_songs(limit=limit)
    if yt_tracks:
        for t in yt_tracks:
            tracks.append({
                "id": f"yt:{t['id']}",
                "name": t["name"],
                "artist": t["artist"],
                "album": t.get("album", ""),
                "image_url": t.get("image_url"),
                "duration_ms": t.get("duration_ms", 0),
                "track_number": 0,
                "sources": [{"provider": "youtube", "id": t["id"]}],
            })

    return _merge_sources(tracks, lambda t: _normalize_key(t["name"], t["artist"]))[:limit]


def get_unified_playlists() -> list[dict]:
    """Merge Spotify + YouTube playlists (not deduped — playlists are source-specific)."""
    playlists = []

    sp_data = sp.get_playlists(limit=50, offset=0)
    if sp_data:
        for p in sp_data.get("playlists", []):
            playlists.append({
                "id": f"sp:{p['id']}",
                "name": p["name"],
                "owner": p.get("owner", ""),
                "image_url": p.get("image_url"),
                "total_tracks": p.get("total_tracks", 0),
                "provider": "spotify",
                "source_id": p["id"],
            })

    yt_playlists = yt.get_playlists()
    if yt_playlists:
        for p in yt_playlists:
            playlists.append({
                "id": f"yt:{p['id']}",
                "name": p["name"],
                "owner": "",
                "image_url": p.get("image_url"),
                "total_tracks": p.get("count", 0),
                "provider": "youtube",
                "source_id": p["id"],
            })

    return playlists


def get_unique_artists() -> list[dict]:
    """Extract unique artists from both libraries."""
    artist_map = defaultdict(lambda: {"name": "", "album_count": 0, "image_url": None, "sources": set()})

    sp_data = sp.get_saved_albums(limit=50, offset=0)
    if sp_data:
        for a in sp_data.get("albums", []):
            key = a["artist"].split(",")[0].strip().lower()
            entry = artist_map[key]
            entry["name"] = a["artist"].split(",")[0].strip()
            entry["album_count"] += 1
            if a.get("image_url"):
                entry["image_url"] = a["image_url"]
            entry["sources"].add("spotify")

    yt_albums = yt.get_library_albums(limit=50)
    if yt_albums:
        for a in yt_albums:
            key = a["artist"].split(",")[0].strip().lower()
            entry = artist_map[key]
            entry["name"] = entry["name"] or a["artist"].split(",")[0].strip()
            entry["album_count"] += 1
            if a.get("image_url") and not entry["image_url"]:
                entry["image_url"] = a["image_url"]
            entry["sources"].add("youtube")

    return [
        {
            "name": v["name"],
            "album_count": v["album_count"],
            "image_url": v["image_url"],
            "sources": list(v["sources"]),
        }
        for v in sorted(artist_map.values(), key=lambda x: x["album_count"], reverse=True)
    ]
