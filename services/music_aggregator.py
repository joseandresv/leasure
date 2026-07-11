"""Unified music aggregator — merges Spotify + YouTube Music libraries."""

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Track
from services import spotify_client as sp
from services import youtube_client as yt

logger = logging.getLogger(__name__)

_RECENCY_TTL = 60  # seconds — shared across the three tab endpoints within one session
_recency_cache: dict = {"ts": 0.0, "data": None}

# Cache for get_unified_recent — the 4-way external API fan-out is ~2s, so repeat
# loads (re-opening the music panel, switching back to the Deck tab) hit this instead.
_RECENT_TTL = 45  # seconds
_recent_cache: dict = {"ts": 0.0, "limit": 0, "data": None}


def _artist_key(artist: str) -> str:
    """Normalize artist name to a lookup key (primary artist only, lowercase)."""
    if not artist:
        return ""
    return artist.split(",")[0].strip().lower()


def _album_key(artist: str, album: str) -> tuple[str, str]:
    return (_artist_key(artist), (album or "").strip().lower())


def _spotify_playlist_id_from_uri(uri: str) -> str | None:
    """Extract the playlist id from a Spotify context URI like 'spotify:playlist:<id>'."""
    if not uri or not uri.startswith("spotify:playlist:"):
        return None
    return uri.split(":", 2)[2]


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


def _compute_entity_recency(limit: int = 50) -> dict:
    """Build artist/album/playlist -> max_ts lookup maps from the same play-history
    sources `get_unified_recent()` uses. Cached in-process for `_RECENCY_TTL` seconds
    so the three tab endpoints share one fetch.

    Returns {'artists': dict, 'albums': dict, 'playlists': dict, '_yt_recent_tracks': list}.
    The `_yt_recent_tracks` entry feeds `_compute_yt_playlist_recency`.
    """
    now_ts = time.time()
    cached = _recency_cache.get("data")
    if cached is not None and now_ts - _recency_cache["ts"] < _RECENCY_TTL:
        logger.info("Recency cache HIT (age %.1fs)", now_ts - _recency_cache["ts"])
        return cached

    t0 = time.perf_counter()
    now = datetime.now(timezone.utc).timestamp()
    artists: dict[str, float] = {}
    albums: dict[tuple[str, str], float] = {}
    playlists: dict[str, float] = {}
    yt_recent_tracks: list[dict] = []

    def _bump(d, key, ts):
        if not key or (isinstance(key, str) and not key.strip()):
            return
        if ts > d.get(key, 0):
            d[key] = ts

    # Fan out the three slow API fetches in parallel — they're independent
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_now = ex.submit(sp.get_currently_playing)
        f_sp_recent = ex.submit(sp.get_recently_played, limit)
        f_yt_history = ex.submit(yt.get_history, limit)
        f_yt_plain = ex.submit(yt.get_youtube_history, limit)

        t_sp_now = time.perf_counter()
        sp_now = f_now.result()
        logger.info("Spotify currently-playing: %.2fs", time.perf_counter() - t_sp_now)

        t_sp_recent = time.perf_counter()
        sp_recent = f_sp_recent.result() or []
        logger.info("Spotify recently-played (%d items): %.2fs", len(sp_recent), time.perf_counter() - t_sp_recent)

        t_yt_hist = time.perf_counter()
        yt_history = f_yt_history.result() or []
        logger.info("YT Music history (%d items): %.2fs", len(yt_history), time.perf_counter() - t_yt_hist)

        t_yt_plain = time.perf_counter()
        yt_plain = f_yt_plain.result() or []
        logger.info("Plain YT history (%d items): %.2fs", len(yt_plain), time.perf_counter() - t_yt_plain)

    if sp_now:
        top_ts = now + 3600
        _bump(artists, _artist_key(sp_now.get("artist", "")), top_ts)
        _bump(albums, _album_key(sp_now.get("artist", ""), sp_now.get("album", "")), top_ts)

    for t in sp_recent:
        ts = _parse_spotify_ts(t.get("played_at", ""))
        _bump(artists, _artist_key(t.get("artist", "")), ts)
        _bump(albums, _album_key(t.get("artist", ""), t.get("album", "")), ts)
        if t.get("context_type") == "playlist":
            pl_id = _spotify_playlist_id_from_uri(t.get("context_uri", ""))
            if pl_id:
                _bump(playlists, f"sp:{pl_id}", ts)

    for i, t in enumerate(yt_history):
        ts = _ytmusic_bucket_ts(t.get("played", ""), i)
        _bump(artists, _artist_key(t.get("artist", "")), ts)
        _bump(albums, _album_key(t.get("artist", ""), t.get("album", "")), ts)
        if t.get("id"):
            yt_recent_tracks.append({"id": t["id"], "_ts": ts})

    for i, t in enumerate(yt_plain):
        ts = now - i * 60
        _bump(artists, _artist_key(t.get("artist", "")), ts)
        if t.get("id"):
            yt_recent_tracks.append({"id": t["id"], "_ts": ts})

    data = {
        "artists": artists,
        "albums": albums,
        "playlists": playlists,
        "_yt_recent_tracks": yt_recent_tracks,
    }
    _recency_cache["ts"] = now_ts
    _recency_cache["data"] = data
    logger.info(
        "Recency compute: %d artists, %d albums, %d sp playlists in %.2fs",
        len(artists), len(albums), len(playlists), time.perf_counter() - t0,
    )
    return data


def _compute_yt_playlist_recency(recent_yt_tracks: list[dict], yt_playlists: list[dict]) -> dict[str, float]:
    """For each YT library playlist, fetch its tracks (in parallel) and return max _ts
    of any overlap with recent YT/YTM history. Playlists with no overlap are omitted.
    """
    if not recent_yt_tracks or not yt_playlists:
        return {}

    recent_ts_by_id: dict[str, float] = {}
    for t in recent_yt_tracks:
        tid = t.get("id")
        ts = t.get("_ts", 0)
        if tid and ts > recent_ts_by_id.get(tid, 0):
            recent_ts_by_id[tid] = ts

    t0 = time.perf_counter()

    def _fetch(pl):
        pl_id = pl.get("id")
        if not pl_id:
            return None
        try:
            data = yt.get_playlist_tracks(pl_id, limit=100)
        except Exception as e:
            logger.warning("YT playlist recency: failed to fetch %s: %s", pl_id, e)
            return None
        if not data:
            return None
        best = 0.0
        for t in data.get("tracks", []):
            ts = recent_ts_by_id.get(t.get("id"))
            if ts and ts > best:
                best = ts
        return (pl_id, best) if best > 0 else None

    result: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(yt_playlists)))) as ex:
        for res in ex.map(_fetch, yt_playlists):
            if res:
                pid, ts = res
                result[f"yt:{pid}"] = ts

    logger.info(
        "YT playlist overlap: %d playlists scanned, %d matched in %.2fs",
        len(yt_playlists), len(result), time.perf_counter() - t0,
    )
    return result


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
    """Merge Spotify saved albums + YouTube library albums, sorted by recency."""
    t0 = time.perf_counter()
    albums = []

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

    merged = _merge_sources(albums, lambda a: _normalize_key(a["name"], a["artist"]))
    recency = _compute_entity_recency()["albums"]
    for a in merged:
        a["_ts"] = recency.get(_album_key(a.get("artist", ""), a.get("name", "")), 0)
    merged.sort(key=lambda a: (-a.get("_ts", 0), a["name"].lower()))
    logger.info("get_unified_albums: %d albums in %.2fs", len(merged), time.perf_counter() - t0)
    return merged


def get_unified_recent(limit: int = 50, force: bool = False) -> list[dict]:
    """Merge Spotify recently played + YouTube Music history + YouTube watch history,
    sorted by actual recency (most recent first).

    Cached in-process for `_RECENT_TTL` seconds (the 4 external fetches cost ~2s).
    Pass force=True to bypass the cache (the UI's Refresh button does this).

    Each track gets a `_ts` recency timestamp:
    - Spotify: real ISO `played_at` timestamp
    - YT Music: approximate timestamp from the 'played' bucket ("Today"/"Yesterday"/date)
    - Plain YT: position-based synthetic timestamp (list order is recency order)
    """
    now_t = time.time()
    if (not force and _recent_cache["data"] is not None
            and _recent_cache["limit"] >= limit
            and now_t - _recent_cache["ts"] < _RECENT_TTL):
        logger.info("get_unified_recent: cache HIT (age %.1fs)", now_t - _recent_cache["ts"])
        return _recent_cache["data"][:limit]

    t0 = time.perf_counter()
    tracks = []
    now = datetime.now(timezone.utc).timestamp()

    # Fan out the 4 slow fetches in parallel
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_now = ex.submit(sp.get_currently_playing)
        f_sp_recent = ex.submit(sp.get_recently_played, limit)
        f_yt_history = ex.submit(yt.get_history, limit)
        f_yt_plain = ex.submit(yt.get_youtube_history, limit)
        sp_now = f_now.result()
        sp_recent = f_sp_recent.result()
        yt_history = f_yt_history.result()
        yt_plain = f_yt_plain.result()

    # Spotify currently playing — always appears at the very top
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
    result = merged[:limit]
    _recent_cache.update({"ts": time.time(), "limit": limit, "data": result})
    logger.info("get_unified_recent: %d tracks merged, returning %d in %.2fs (cache MISS)",
                len(merged), len(result), time.perf_counter() - t0)
    return result


def get_unified_playlists() -> list[dict]:
    """Merge Spotify + YouTube playlists (not deduped — playlists are source-specific),
    sorted by recency (Spotify via play context; YT via recent-track overlap)."""
    t0 = time.perf_counter()
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

    yt_raw = yt.get_playlists() or []
    for p in yt_raw:
        playlists.append({
            "id": f"yt:{p['id']}",
            "name": p["name"],
            "owner": "",
            "image_url": p.get("image_url"),
            "total_tracks": p.get("count", 0),
            "provider": "youtube",
            "source_id": p["id"],
        })

    recency_data = _compute_entity_recency()
    recency = dict(recency_data["playlists"])  # copy so we can add YT entries
    yt_pl_recency = _compute_yt_playlist_recency(recency_data["_yt_recent_tracks"], yt_raw)
    recency.update(yt_pl_recency)

    for p in playlists:
        p["_ts"] = recency.get(p["id"], 0)
    playlists.sort(key=lambda p: (-p.get("_ts", 0), p["name"].lower()))
    logger.info("get_unified_playlists: %d playlists in %.2fs", len(playlists), time.perf_counter() - t0)
    return playlists


def get_unique_artists() -> list[dict]:
    """Extract unique artists from both libraries, sorted by recency (most recently
    played first, unplayed artists alphabetical at the bottom)."""
    t0 = time.perf_counter()
    artist_map = defaultdict(lambda: {"name": "", "album_count": 0, "image_url": None, "sources": set()})

    sp_data = sp.get_saved_albums(limit=50, offset=0)
    if sp_data:
        for a in sp_data.get("albums", []):
            key = _artist_key(a["artist"])
            entry = artist_map[key]
            entry["name"] = a["artist"].split(",")[0].strip()
            entry["album_count"] += 1
            if a.get("image_url"):
                entry["image_url"] = a["image_url"]
            entry["sources"].add("spotify")

    yt_albums = yt.get_library_albums(limit=50)
    if yt_albums:
        for a in yt_albums:
            key = _artist_key(a["artist"])
            entry = artist_map[key]
            entry["name"] = entry["name"] or a["artist"].split(",")[0].strip()
            entry["album_count"] += 1
            if a.get("image_url") and not entry["image_url"]:
                entry["image_url"] = a["image_url"]
            entry["sources"].add("youtube")

    recency = _compute_entity_recency()["artists"]
    artists = [
        {
            "name": v["name"],
            "album_count": v["album_count"],
            "image_url": v["image_url"],
            "sources": list(v["sources"]),
            "_ts": recency.get(k, 0),
        }
        for k, v in artist_map.items()
    ]
    artists.sort(key=lambda a: (-a.get("_ts", 0), a["name"].lower()))
    logger.info("get_unique_artists: %d artists in %.2fs", len(artists), time.perf_counter() - t0)
    return artists
