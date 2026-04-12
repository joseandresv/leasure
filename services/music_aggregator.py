"""Unified music aggregator — merges Spotify + YouTube Music libraries."""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Track
from services import spotify_client as sp
from services import youtube_client as yt

logger = logging.getLogger(__name__)


def _parse_spotify_ts(played_at: str) -> float:
    """Parse a Spotify ISO-8601 played_at string into a unix timestamp."""
    if not played_at:
        return 0.0
    try:
        # Spotify uses "2026-04-11T22:18:32.488Z"
        dt = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _ytmusic_bucket_ts(played: str, position: int) -> float:
    """Convert a YouTube Music 'played' bucket label + position to an approximate unix timestamp.

    YT Music history groups items coarsely ("Today", "Yesterday", "This week", etc.).
    Within a bucket we use the list position as a tiebreaker (earlier position = more recent).
    """
    now = datetime.now(timezone.utc).timestamp()
    day = 86400
    bucket = (played or "").strip().lower()

    if bucket in ("today", ""):
        base = now
    elif bucket == "yesterday":
        base = now - day
    elif bucket in ("this week", "last week"):
        base = now - 3 * day
    elif bucket == "this month":
        base = now - 14 * day
    else:
        # Try to parse an actual date like "Nov 24, 2023"
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(bucket, fmt).replace(tzinfo=timezone.utc)
                base = dt.timestamp()
                break
            except ValueError:
                continue
        else:
            base = now - 30 * day  # unknown — treat as old

    # Subtract a tiny offset for position so list order is preserved within bucket
    return base - position * 0.001


def _normalize_key(title: str, artist: str) -> str:
    """Create a dedup key from title + artist."""
    return f"{title.strip().lower()}|{artist.strip().lower().split(',')[0]}"


def _merge_sources(items: list[dict], key_fn) -> list[dict]:
    """Deduplicate items by key, merging their sources lists.

    If items carry a `_ts` field (recency timestamp), the merged entry keeps
    the maximum timestamp across all duplicates so the most-recent play wins.
    """
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
            # Keep the most recent timestamp across duplicates
            if "_ts" in item and item["_ts"] > existing.get("_ts", 0):
                existing["_ts"] = item["_ts"]
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
            elif src["provider"] in ("youtube", "youtube music") and src.get("id"):
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
    """Merge Spotify recently played + YouTube Music history + YouTube watch history,
    sorted by actual recency (most recent first).

    Each track gets a `_ts` recency timestamp:
    - Spotify: real ISO `played_at` timestamp
    - YT Music: approximate timestamp from the 'played' bucket ("Today"/"Yesterday"/date)
    - Plain YT: position-based synthetic timestamp (list order is recency order)
    """
    tracks = []
    now = datetime.now(timezone.utc).timestamp()

    # Spotify currently playing — always appears at the very top
    sp_now = sp.get_currently_playing()
    if sp_now:
        tracks.append({
            "id": f"sp:{sp_now['id']}",
            "name": sp_now["name"],
            "artist": sp_now["artist"],
            "album": sp_now.get("album", ""),
            "image_url": sp_now.get("album_image_url"),
            "duration_ms": sp_now.get("duration_ms", 0),
            "track_number": sp_now.get("track_number", 0),
            "sources": [{"provider": "spotify", "id": sp_now["id"], "uri": sp_now.get("uri", ""),
                         "artist_id": sp_now.get("artist_id", "")}],
            # Future-dated so it wins even if plain YT is hammering "now" timestamps
            "_ts": now + 3600,
        })

    # Spotify recently played — real timestamps
    sp_recent = sp.get_recently_played(limit=limit)
    if sp_recent:
        for t in sp_recent:
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
                "_ts": _parse_spotify_ts(t.get("played_at", "")),
            })

    # YouTube Music history — approximate timestamps from the "played" bucket
    yt_history = yt.get_history(limit=limit)
    if yt_history:
        for i, t in enumerate(yt_history):
            tracks.append({
                "id": f"ytm:{t['id']}",
                "name": t["name"],
                "artist": t["artist"],
                "album": t.get("album", ""),
                "image_url": t.get("image_url"),
                "duration_ms": t.get("duration_ms", 0),
                "track_number": 0,
                "sources": [{"provider": "youtube music", "id": t["id"]}],
                "_ts": _ytmusic_bucket_ts(t.get("played", ""), i),
            })

    # Plain YouTube watch history — position-based (no real timestamps available)
    yt_plain = yt.get_youtube_history(limit=limit)
    if yt_plain:
        for i, t in enumerate(yt_plain):
            tracks.append({
                "id": f"yt:{t['id']}",
                "name": t["name"],
                "artist": t["artist"],
                "album": t.get("album", ""),
                "image_url": t.get("image_url"),
                "duration_ms": t.get("duration_ms", 0),
                "track_number": 0,
                "sources": [{"provider": "youtube", "id": t["id"]}],
                "_ts": now - i * 60,  # assume ~1 minute between items, list order = recency
            })

    merged = _merge_sources(tracks, lambda t: _normalize_key(t["name"], t["artist"]))
    # Sort by recency timestamp (most recent first)
    merged.sort(key=lambda t: t.get("_ts", 0), reverse=True)
    return merged[:limit]


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
