"""Check what Spotify answers for the saved-library reads (liked songs, saved albums, playlists,
followed artists) before Leasure relies on them.

    python -m scripts.spotify_saved_check

About 9 Web API calls, at least 3 s apart, through Leasure's rate-limit guard. Prints counts, yes/no
answers, HTTP statuses and field names only: no titles, names, ids or tokens. Refuses to start while
Spotify is rate-limiting Leasure, and stops at the first 429 without retrying.

Exits 0 when liked songs, saved albums and playlists answer 200 and at least one liked song parses;
1 otherwise (a rate limit included); 2 when Spotify is not connected.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
import spotipy  # noqa: E402

from config import settings  # noqa: E402
from services import spotify_client as sp  # noqa: E402

NOT_CONNECTED = "Spotify is not connected. Open Leasure, connect Spotify on Home, then run this again."
CONTAINS_ENDPOINT = "me/library/contains"  # what spotipy 2.26's current_user_saved_tracks_contains calls


class _RateLimited(Exception):
    pass


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _first_item(page: sp.SavedPage | None) -> dict:
    items = page.raw.get("items") if page is not None and isinstance(page.raw.get("items"), list) else []
    return _dict(items[0]) if items else {}


class Probe:
    def __init__(self, pacer: sp.SyncPacer) -> None:
        self.pacer = pacer
        self.calls = 0

    def call(self, fn) -> tuple[int | str, object]:
        """(HTTP status, result). A 429, or the guard refusing to send, stops the whole check."""
        self.pacer.wait()
        self.calls += 1
        try:
            result = fn()
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                raise _RateLimited from None
            return e.http_status, None
        except requests.RequestException:
            return "network_error", None
        if result is None and sp.is_rate_limited():
            raise _RateLimited
        return 200, result


def _liked(probe: Probe, client, report: dict) -> list[str]:
    status, page = probe.call(lambda: sp.saved_tracks_page(0, 2, client=client))
    first = _first_item(page)
    key = next((k for k in ("item", "track") if isinstance(first.get(k), dict)), None)
    track = _dict(first.get(key)) if key else {}
    report["liked"] = {"status": status, "total": page.total if page else None, "item_key": key,
                       "has_external_ids": "external_ids" in track,
                       "has_isrc": bool(_dict(track.get("external_ids")).get("isrc")),
                       "parsed_items": len(page.items) if page else 0}
    return [t["id"] for t in (page.items if page else []) if t.get("id")][:2]


def _albums(probe: Probe, client, report: dict) -> None:
    status, page = probe.call(lambda: sp.saved_albums_page(0, 1, client=client))
    album = _dict(_first_item(page).get("album"))
    report["albums"] = {"status": status, "total": page.total if page else None,
                        "album_has_tracks": isinstance(_dict(album.get("tracks")).get("items"), list) if album else None}


def _playlists(probe: Probe, client, report: dict) -> None:
    _, me = probe.call(client.current_user)
    my_id = _dict(me).get("id")
    status, page = probe.call(lambda: sp.user_playlists_page(0, 50, client=client))
    first = _first_item(page)
    count_key = next((k for k in ("items", "tracks") if "total" in _dict(first.get(k))), None)
    playlists = page.items if page else []
    owned = [p for p in playlists if my_id and p["owner_id"] == my_id]
    followed = [p for p in playlists if p not in owned]
    report["playlists"] = {"status": status, "total": page.total if page else None, "count_key": count_key,
                           "owned_in_first_50": len(owned), "followed_in_first_50": len(followed),
                           "user_id_known": my_id is not None}
    for name, group in (("owned", owned), ("followed", followed)):
        report[f"{name}_items_status"] = probe.call(
            lambda group=group: sp.playlist_items_page(group[0]["id"], 0, 1, client=client))[0] if group else "skipped"


def _cached_scopes() -> list[str] | None:
    try:
        token = spotipy.CacheFileHandler(cache_path=sp.CACHE_PATH).get_cached_token()
    except Exception:
        return None
    scope = _dict(token).get("scope")
    return sorted(scope.split()) if isinstance(scope, str) else None


def run(client, probe: Probe, report: dict) -> None:
    """Fills `report` step by step, so a rate limit midway still shows what was learnt."""
    ids = _liked(probe, client, report)
    report["batch_tracks_status"] = probe.call(lambda: client.tracks(ids))[0] if ids else "skipped"
    _albums(probe, client, report)
    _playlists(probe, client, report)
    report["contains_status"] = probe.call(lambda: client.current_user_saved_tracks_contains(ids[:1]))[0] if ids else "skipped"
    report["contains_endpoint"] = CONTAINS_ENDPOINT
    report["follow_scope"] = probe.call(lambda: sp.follow_scope_status(client=client))[1]
    report["cached_scopes (requested, not proof of grant)"] = _cached_scopes()


def _failures(report: dict) -> list[str]:
    failed = [name for name in ("liked", "albums", "playlists") if _dict(report.get(name)).get("status") != 200]
    if not _dict(report.get("liked")).get("parsed_items"):
        failed.append("liked_parsed (no liked song could be read)")
    return failed


def main(argv: list[str] | None = None, *, pacer: sp.SyncPacer | None = None) -> int:
    argparse.ArgumentParser(description="Check what Spotify answers for Leasure's saved-library reads.").parse_args(argv)
    # spotipy logs every HTTP error with its URL, which carries playlist ids.
    logging.getLogger("spotipy").setLevel(logging.CRITICAL)
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        print(NOT_CONNECTED)
        return 2
    if sp.is_rate_limited():
        print(f"{sp.rate_limit_message()}. Nothing was sent; run this again after that.")
        return 1
    client = sp.get_client()
    if client is None:
        print(NOT_CONNECTED)
        return 2

    probe = Probe(pacer or sp.SyncPacer())
    report: dict = {}
    failed: list[str]
    try:
        run(client, probe, report)
        failed = _failures(report)
    except _RateLimited:
        failed = ["rate_limited"]
    report["calls_sent"] = probe.calls
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if "rate_limited" in failed:
        print(f"{sp.rate_limit_message() or 'Spotify is rate-limiting Leasure'}. Stopped without retrying.")
    print("FAIL: " + ", ".join(failed) if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
