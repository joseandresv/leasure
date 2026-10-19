"""Check what Spotify answers for the saved-library reads (liked songs, saved albums, playlists,
followed artists) before Leasure relies on them.

    python -m scripts.spotify_saved_check

9 Web API calls, at least 3 s apart, through Leasure's rate-limit guard; a timed-out saved-albums or
playlists page is read once more with a smaller page, so at most 11. Prints counts, yes/no answers, HTTP
statuses, field names and, per call, a diagnosis (exception class names, timeout yes/no, seconds taken)
only: no titles, names, ids, URLs, messages or tokens. Refuses to start while Spotify is rate-limiting
Leasure, and stops at the first 429 without retrying.

Exits 0 when liked songs, saved albums and playlists answer 200 and at least one liked song parses;
1 otherwise (a rate limit included); 2 when Spotify is not connected.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
import spotipy  # noqa: E402

from config import settings  # noqa: E402
from services import spotify_client as sp  # noqa: E402

NOT_CONNECTED = "Spotify is not connected. Open Leasure, connect Spotify on Home, then run this again."
CONTAINS_ENDPOINT = "me/library/contains"  # what spotipy 2.26's current_user_saved_tracks_contains calls
MAX_CALLS = 11
RETRY_PAGE_LIMIT = 10
CHAIN_DEPTH = 6


class _RateLimited(Exception):
    pass


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _first_item(page: sp.SavedPage | None) -> dict:
    items = page.raw.get("items") if page is not None and isinstance(page.raw.get("items"), list) else []
    return _dict(items[0]) if items else {}


def _class_name(e: BaseException) -> str:
    cls = type(e)
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def _wrapped(e: BaseException) -> BaseException | None:
    # requests re-raises urllib3 errors as its first argument (a body-read timeout becomes a ConnectionError);
    # urllib3's MaxRetryError keeps the underlying error in `reason`.
    for inner in (e.__cause__, e.args[0] if e.args else None, getattr(e, "reason", None), e.__context__):
        if isinstance(inner, BaseException):
            return inner
    return None


def _exception_chain(e: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = e
    while current is not None and current not in chain and len(chain) < CHAIN_DEPTH:
        chain.append(current)
        current = _wrapped(current)
    return chain


def _diagnose(e: BaseException, started: float) -> dict:
    """Class names, status and timing only: exception messages carry request URLs, and Spotify's carry ids."""
    chain = _exception_chain(e)
    response = getattr(e, "response", None)
    status = getattr(e, "http_status", None) or getattr(response, "status_code", None)
    return {"error": _class_name(e), "causes": [_class_name(c) for c in chain[1:]],
            "timeout": any("Timeout" in type(c).__name__ for c in chain),
            "http_status": status if isinstance(status, int) else None,
            "elapsed_s": round(time.monotonic() - started, 2)}


class Probe:
    def __init__(self, pacer: sp.SyncPacer) -> None:
        self.pacer = pacer
        self.calls = 0
        self.diagnosis: dict[str, dict] = {}

    def call(self, step: str, fn) -> tuple[int | str, object]:
        """(HTTP status, result), diagnosed under `step`. A 429, or the guard refusing to send, stops the whole check."""
        if self.calls >= MAX_CALLS:
            self.diagnosis[step] = {"skipped": "call budget spent"}
            return "skipped", None
        self.pacer.wait()
        self.calls += 1
        started = time.monotonic()
        try:
            result = fn()
        except spotipy.SpotifyException as e:
            self.diagnosis[step] = _diagnose(e, started)
            if e.http_status == 429:
                raise _RateLimited from None
            return e.http_status, None
        except requests.RequestException as e:
            self.diagnosis[step] = _diagnose(e, started)
            return "network_error", None
        self.diagnosis[step] = {"elapsed_s": round(time.monotonic() - started, 2)}
        if result is None and sp.is_rate_limited():
            raise _RateLimited
        return 200, result

    def page(self, step: str, limit: int, fetch) -> tuple[int | str, sp.SavedPage | None, dict | None]:
        """(status, page, retry report). A timed-out page is read once more at no more than RETRY_PAGE_LIMIT items."""
        status, page = self.call(step, lambda: fetch(limit))
        if not self.diagnosis[step].get("timeout"):
            return status, page, None
        retry_limit = min(limit, RETRY_PAGE_LIMIT)
        retry_status, retry_page = self.call(f"{step}_retry", lambda: fetch(retry_limit))
        return status, page, {"limit": retry_limit, "status": retry_status,
                              "total": retry_page.total if retry_page else None}


def _liked(probe: Probe, client, report: dict) -> list[str]:
    status, page = probe.call("liked", lambda: sp.saved_tracks_page(0, 2, client=client))
    first = _first_item(page)
    key = next((k for k in ("item", "track") if isinstance(first.get(k), dict)), None)
    track = _dict(first.get(key)) if key else {}
    report["liked"] = {"status": status, "total": page.total if page else None, "item_key": key,
                       "has_external_ids": "external_ids" in track,
                       "has_isrc": bool(_dict(track.get("external_ids")).get("isrc")),
                       "parsed_items": len(page.items) if page else 0}
    return [t["id"] for t in (page.items if page else []) if t.get("id")][:2]


def _albums(probe: Probe, client, report: dict) -> None:
    status, page, retry = probe.page("albums", 1, lambda limit: sp.saved_albums_page(0, limit, client=client))
    album = _dict(_first_item(page).get("album"))
    report["albums"] = {"status": status, "total": page.total if page else None,
                        "album_has_tracks": isinstance(_dict(album.get("tracks")).get("items"), list) if album else None}
    if retry:
        report["albums_retry"] = retry


def _playlists(probe: Probe, client, report: dict) -> None:
    _, me = probe.call("me", client.current_user)
    my_id = _dict(me).get("id")
    status, page, retry = probe.page("playlists", 50, lambda limit: sp.user_playlists_page(0, limit, client=client))
    first = _first_item(page)
    count_key = next((k for k in ("items", "tracks") if "total" in _dict(first.get(k))), None)
    playlists = page.items if page else []
    owned = [p for p in playlists if my_id and p["owner_id"] == my_id]
    followed = [p for p in playlists if p not in owned]
    report["playlists"] = {"status": status, "total": page.total if page else None, "count_key": count_key,
                           "owned_in_first_50": len(owned), "followed_in_first_50": len(followed),
                           "user_id_known": my_id is not None}
    if retry:
        report["playlists_retry"] = retry
    for name, group in (("owned", owned), ("followed", followed)):
        report[f"{name}_items_status"] = probe.call(
            f"{name}_items", lambda group=group: sp.playlist_items_page(group[0]["id"], 0, 1, client=client))[0] if group else "skipped"


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
    report["batch_tracks_status"] = probe.call("batch_tracks", lambda: client.tracks(ids))[0] if ids else "skipped"
    _albums(probe, client, report)
    _playlists(probe, client, report)
    report["contains_status"] = probe.call("contains", lambda: client.current_user_saved_tracks_contains(ids[:1]))[0] if ids else "skipped"
    report["contains_endpoint"] = CONTAINS_ENDPOINT
    report["follow_scope"] = probe.call("follow_scope", lambda: sp.follow_scope_status(client=client))[1]
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
    report["diagnosis"] = probe.diagnosis
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if "rate_limited" in failed:
        print(f"{sp.rate_limit_message() or 'Spotify is rate-limiting Leasure'}. Stopped without retrying.")
    print("FAIL: " + ", ".join(failed) if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
