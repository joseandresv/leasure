"""Review fixes for the saved-library build step: anonymiser slugs, per-item 403s, signing per endpoint,
page-size step-down, untested signing, null track fields, 429 without Retry-After, follow 403 labels."""
import json

import httpx
import pytest
import spotipy

from scripts import qobuz_check
from services import qobuz_client as qc
from services import qobuz_session
from services import spotify_client as sp
from tests.qobuz_fakes import store_session
from tests.test_qobuz_client import S_BERLIN, Routes, cache_payload, make_client, reply
from tests.test_qobuz_library import fav_track, favorites_answer
from tests.test_saved_check_scripts import (  # noqa: F401 (fixtures)
    q_favorites,
    q_track,
    qobuz_routes,
    run_favorites,
    run_spotify,
    spotify_report,
    spotify_script,
)
from tests.test_spotify_saved import FakeSpotify


def report_of(out: str) -> dict:
    return json.loads(out[:out.rindex("}") + 1].split("Verdict written")[0])


def use_api(tmp_path, monkeypatch, api, *, cache=True) -> None:
    store_session()
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: make_client(tmp_path, api, cache=cache))
    monkeypatch.setattr(qobuz_check, "FAVORITES_CHECK_PATH", tmp_path / "check.json")


def test_the_anonymiser_keeps_genre_slugs_only():
    out = qobuz_check.anonymise({"albums": {"items": [{
        "id": "abc123", "title": "Discovery", "slug": "discovery-daft-punk",
        "artist": {"id": 36819, "name": "Daft Punk", "slug": "daft-punk"},
        "label": {"slug": "parlophone"}, "genre": {"slug": "electro"}, "release_type": "album"}]}})

    (album,) = out["albums"]["items"]
    assert album["slug"] == album["artist"]["slug"] == album["label"]["slug"] == "<str>"
    assert album["genre"]["slug"] == "electro" and album["release_type"] == "album"


@pytest.mark.parametrize("endpoint", ["playlist/get", "album/get"])
def test_a_403_on_one_item_probe_keeps_the_verdict(tmp_path, monkeypatch, capsys, endpoint):
    use_api(tmp_path, monkeypatch, qobuz_routes(**{endpoint: [reply(403)]}))

    code, out = run_favorites(capsys)

    verdict = json.loads((tmp_path / "check.json").read_text())
    assert code == 0 and out.rstrip().endswith("PASS")
    assert verdict["tracks_ok"] and verdict["albums_ok"] and verdict["playlists_ok"]
    assert verdict["playlist_get_ok"] is (endpoint != "playlist/get")
    assert f"{endpoint}: HTTP 403 (QobuzForbiddenError)" in report_of(out)["errors"]


def test_a_403_on_a_list_endpoint_still_stops_the_check(tmp_path, monkeypatch, capsys):
    use_api(tmp_path, monkeypatch, qobuz_routes(**{"playlist/getUserPlaylists": [reply(403)]}))

    code, out = run_favorites(capsys)

    assert code == 1 and "QobuzForbiddenError" in out and not (tmp_path / "check.json").exists()


def test_the_signing_mode_is_learned_per_endpoint(tmp_path):
    def favorites(request):
        if "request_sig" not in request.url.params:
            return httpx.Response(401)
        return httpx.Response(200, json=favorites_answer("tracks", []))

    def playlists(request):
        if "request_sig" in request.url.params:
            return httpx.Response(400, json={"message": "Invalid Request Signature parameter (request_sig)"})
        return httpx.Response(200, json={"playlists": {"items": [], "total": 0}})

    api = Routes({"favorite/getUserFavorites": [favorites], "playlist/getUserPlaylists": [playlists]})
    client = make_client(tmp_path, api, cache=cache_payload(secret=S_BERLIN))

    client.favorites("tracks")
    client.user_playlists()
    client.favorites("tracks")

    assert client.user_playlists().total == 0
    sent = [(r.url.path.rsplit("/", 1)[1], "request_sig" in r.url.params) for r in api.requests]
    assert sent == [("getUserFavorites", False), ("getUserFavorites", True), ("getUserPlaylists", False),
                    ("getUserFavorites", True), ("getUserPlaylists", False)]


def test_a_limit_refusal_steps_down_even_when_the_signed_retry_rejects_the_signature(tmp_path):
    def handler(request):
        if "request_sig" in request.url.params:
            return httpx.Response(400, json={"message": "Invalid Request Signature parameter (request_sig)"})
        if request.url.params["limit"] == "500":
            return httpx.Response(400, json={"message": "Invalid argument: limit"})
        return httpx.Response(200, json=favorites_answer("tracks", [fav_track(1)]))
    api = Routes({"favorite/getUserFavorites": [handler]})
    client = make_client(tmp_path, api, cache=cache_payload(secret=S_BERLIN))

    pages = list(client.iter_favorites("tracks"))

    assert len(pages) == 1 and client.library_page_size == qc.LIBRARY_FALLBACK_PAGE_SIZE


def test_the_report_says_signing_was_not_tried_without_a_proven_secret(tmp_path, monkeypatch, capsys):
    use_api(tmp_path, monkeypatch, qobuz_routes(**{"favorite/getUserFavorites": [reply(401)]}))

    _, out = run_favorites(capsys)

    report = report_of(out)
    assert report["signing"] == {"unsigned_status": 401, "signed_status": None, "needs_signing": None,
                                 "secret_available": False}
    assert any("signing not tried" in e for e in report["errors"])


def test_track_fields_tell_a_null_value_from_a_missing_key(tmp_path, monkeypatch, capsys):
    def favorites(request):
        if request.url.params["type"] != "tracks":
            return q_favorites(request)
        items = [{**q_track(1), "version": None}, {k: v for k, v in q_track(2).items() if k != "isrc"}]
        return httpx.Response(200, json={"tracks": {"items": items, "total": 2, "limit": 5, "offset": 0}})
    use_api(tmp_path, monkeypatch, qobuz_routes(**{"favorite/getUserFavorites": [favorites]}))

    _, out = run_favorites(capsys)

    report = report_of(out)
    assert report["track_fields"]["version"] is True and report["track_fields"]["isrc"] is False
    assert report["track_fields_with_value"]["version"] == 1 and report["track_fields_with_value"]["isrc"] == 1


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "0"}])
def test_spotify_check_stops_at_a_429_without_retry_after(spotify_script, capsys, headers):  # noqa: F811
    spotify_script.fail["liked"] = spotipy.SpotifyException(429, -1, "slow down", headers=headers)

    code, out = run_spotify(capsys)

    assert code == 1 and "FAIL: rate_limited" in out and spotify_script.calls == ["liked"]


def test_a_429_without_retry_after_still_opens_a_window(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_rate_limited_until", 0.0)
    monkeypatch.setattr(sp, "RATE_LIMIT_PATH", tmp_path / "spotify_rate_limit.json")
    fake = FakeSpotify({"tracks": {0: spotipy.SpotifyException(429, -1, "slow down", headers={})}})

    listing = sp.fetch_saved_tracks(client=fake)

    assert listing.stopped == "rate_limited" and sp.is_rate_limited()


def test_followed_artists_label_a_plain_403_refused_like_the_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_rate_limited_until", 0.0)
    forbidden = spotipy.SpotifyException(403, -1, "https://api.spotify.com/v1/me/following:\n Forbidden")
    fake = FakeSpotify(follow={None: forbidden})

    assert sp.follow_scope_status(client=fake) == "refused"
    assert sp.fetch_followed_artists(client=fake).stopped == "refused"
