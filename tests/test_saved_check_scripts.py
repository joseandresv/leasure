"""scripts/qobuz_check.py --favorites and scripts/spotify_saved_check.py against scripted services (no network)."""
import json
import re
import time
from pathlib import Path

import httpx
import pytest
import spotipy

from config import settings
from scripts import qobuz_check, spotify_saved_check
from services import qobuz_session
from services import spotify_client as sp
from tests.qobuz_fakes import TOKEN, store_session
from tests.test_qobuz_client import Routes, fixture, make_client, reply

TITLE, ARTIST = "Very Private Title", "Very Private Artist"
COVER = "https://static.qobuz.com/images/covers/12/34/1234567890_600.jpg"
QOBUZ_IDS = ("770001", "770002", "alb-770003", "880001")


def q_track(n: int) -> dict:
    return {"id": 770000 + n, "title": TITLE, "version": "Live", "isrc": "USSM15900113", "streamable": True,
            "maximum_bit_depth": 24, "duration": 200, "favorited_at": 1760000000 - n,
            "performer": {"id": 31, "name": ARTIST},
            "album": {"id": "alb-770003", "title": TITLE, "image": {"large": COVER}, "artist": {"name": ARTIST},
                      "genre": {"slug": "jazz", "name": "Jazz"}, "release_type": "album"}}


def q_favorites(request: httpx.Request) -> httpx.Response:
    kind, limit = request.url.params["type"], int(request.url.params["limit"])
    items = {"tracks": [q_track(1), q_track(2)][:limit],
             "albums": [{"id": "alb-770003", "title": TITLE, "artist": {"name": ARTIST}, "image": {"large": COVER}}],
             "artists": [{"id": 31, "name": ARTIST}]}[kind]
    total = {"tracks": 7, "albums": 1, "artists": 1}[kind]
    return httpx.Response(200, json={kind: {"items": items, "total": total, "limit": limit, "offset": 0}})


def qobuz_routes(**overrides) -> Routes:
    playlists = {"playlists": {"items": [{"id": 880001, "name": TITLE, "tracks_count": 2, "updated_at": 1760000000,
                                          "owner": {"id": 1234567, "name": ARTIST}}], "total": 1}}
    routes = {
        "user/login": [reply(200, fixture("user_login.json"))],
        "favorite/getUserFavorites": [q_favorites],
        "favorite/getUserFavoriteIds": [reply(200, {"tracks": list(range(7)), "albums": ["a"], "artists": [31]})],
        "playlist/getUserPlaylists": [reply(200, playlists)],
        "playlist/get": [reply(200, {"id": 880001, "name": TITLE, "tracks": {"items": [q_track(1)], "total": 1}})],
        "album/get": [reply(200, {"id": "alb-770003", "title": TITLE, "artist": {"name": ARTIST},
                                  "tracks": {"items": [{"id": 1, "title": TITLE}], "total": 1}})],
    }
    routes.update(overrides)
    return Routes(routes)


@pytest.fixture
def qobuz_api(tmp_path, monkeypatch):
    store_session()
    api = qobuz_routes()
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: make_client(tmp_path, api))
    monkeypatch.setattr(qobuz_check, "FAVORITES_CHECK_PATH", tmp_path / "qobuz_favorites_check.json")
    return api


def run_favorites(capsys, *extra) -> tuple[int, str]:
    code = qobuz_check.main(["--favorites", *extra])
    return code, capsys.readouterr().out


def test_favorites_check_passes_and_makes_no_search_or_stream_call(qobuz_api, capsys):
    code, out = run_favorites(capsys)

    assert code == 0 and out.rstrip().endswith("PASS")
    called = {r.url.path.removeprefix("/api.json/0.2/") for r in qobuz_api.requests}
    assert not called & {"track/search", "track/getFileUrl", "track/get"}
    assert len(qobuz_api.requests) <= 12


def test_favorites_report_has_counts_and_booleans_but_no_titles_ids_or_secrets(qobuz_api, capsys):
    _, out = run_favorites(capsys)

    assert TITLE not in out and ARTIST not in out and TOKEN not in out and "static.qobuz.com" not in out
    assert not any(i in out for i in QOBUZ_IDS) and "1234567" not in out and "…4321" in out
    report = json.loads(out[:out.rindex("}") + 1].split("Verdict written")[0])
    assert report["signing"] == {"unsigned_status": 200, "signed_status": None, "needs_signing": False,
                                 "secret_available": False}
    assert report["favorites"]["tracks"]["total"] == 7 and report["page_cap"] == 2
    assert report["ids_complete"] == {"tracks": True, "albums": True, "artists": True}
    assert report["track_fields"] == dict.fromkeys(qobuz_check.TRACK_FIELDS, True)
    assert report["has_favorited_at"] and report["newest_first"]
    assert report["playlists"]["filter_owner_ok"] and report["playlists"]["has_updated_at"]
    assert report["album_get"] == {"tracks_total": 1, "tracks_complete": True, "track_items_have_album": False}


def test_favorites_verdict_file_holds_booleans_only(qobuz_api, capsys):
    run_favorites(capsys)

    verdict = json.loads(qobuz_check.FAVORITES_CHECK_PATH.read_text())
    assert {k for k, v in verdict.items() if isinstance(v, bool)} == {
        "tracks_ok", "albums_ok", "artists_ok", "playlists_ok", "playlist_get_ok", "ids_trusted", "signed"}
    assert verdict["page_size"] == 500 and set(verdict) - {"checked_at", "page_size"} <= {
        "tracks_ok", "albums_ok", "artists_ok", "playlists_ok", "playlist_get_ok", "ids_trusted", "signed"}


def test_a_favorites_kind_in_an_unknown_shape_fails_and_is_named(tmp_path, monkeypatch, capsys):
    store_session()

    def broken_albums(request):
        if request.url.params["type"] == "albums":
            return httpx.Response(200, json={"albums": "nope"})
        return q_favorites(request)

    api = qobuz_routes(**{"favorite/getUserFavorites": [broken_albums]})
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: make_client(tmp_path, api))
    monkeypatch.setattr(qobuz_check, "FAVORITES_CHECK_PATH", tmp_path / "check.json")

    code, out = run_favorites(capsys)

    assert code == 1 and out.rstrip().endswith("FAIL: albums_ok")
    assert json.loads((tmp_path / "check.json").read_text())["albums_ok"] is False


def test_a_rate_limit_stops_the_favorites_check_without_a_verdict(tmp_path, monkeypatch, capsys):
    store_session()
    api = qobuz_routes(**{"favorite/getUserFavorites": [reply(429, headers={"Retry-After": "60"})]})
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: make_client(tmp_path, api))
    monkeypatch.setattr(qobuz_check, "FAVORITES_CHECK_PATH", tmp_path / "check.json")

    code, out = run_favorites(capsys)

    assert code == 1 and "QobuzRateLimitError" in out and not (tmp_path / "check.json").exists()
    assert len(api.to("getUserFavorites")) == 1


def test_favorites_without_a_sign_in_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: pytest.fail("no client expected"))

    assert qobuz_check.main(["--favorites"]) == 2


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def test_saved_favorites_fixtures_keep_only_placeholders_and_enum_values(qobuz_api, capsys, tmp_path):
    folder = tmp_path / "fixtures"
    run_favorites(capsys, "--save-fixtures", str(folder))

    written = {p.name: json.loads(p.read_text()) for p in folder.iterdir()}
    assert {"favorites_tracks.json", "favorite_ids.json", "user_playlists.json", "album_get.json"} <= set(written)
    strings = [s for data in written.values() for s in _strings(data)]
    allowed = re.compile(r"^(<str>|\d+|[a-z][a-z_-]{0,31})$")
    assert strings and all(allowed.match(s) for s in strings)
    assert {"jazz", "album"} <= set(strings) and not any("http" in s for s in strings)
    text = json.dumps(written)
    assert not any(i in text for i in QOBUZ_IDS) and "1234567" not in text


def test_the_anonymiser_maps_the_same_id_to_the_same_fake():
    out = qobuz_check.anonymise({"a": {"id": 5, "album_id": "x"}, "b": {"id": 5}, "tracks": [5, 6], "duration": 9})

    assert out["a"]["id"] == out["b"]["id"] == out["tracks"][0] != out["tracks"][1]
    assert out["a"]["album_id"] == str(qobuz_check.FAKE_ID_BASE + 2) and out["duration"] == 9


# --- spotify_saved_check ---------------------------------------------------------------------------

def s_track(n: int) -> dict:
    return {"id": f"sp{n}id", "uri": f"spotify:track:sp{n}id", "name": TITLE, "type": "track",
            "artists": [{"id": "artistid", "name": ARTIST}], "external_ids": {"isrc": "USUM70409983"},
            "album": {"id": "albumid", "name": TITLE, "images": []}}


class ScriptSpotify:
    def __init__(self, fail: dict | None = None):
        self.fail = fail or {}
        self.calls: list[str] = []

    def _go(self, name, answer):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]
        return answer

    def current_user_saved_tracks(self, limit=20, offset=0, market=None):
        return self._go("liked", {"items": [{"added_at": "2026-01-01T00:00:00Z", "item": s_track(1)},
                                            {"added_at": "2026-01-01T00:00:00Z", "item": s_track(2)}],
                                  "total": 2914, "next": "https://api.spotify.com/next"})

    def tracks(self, ids, market=None):
        return self._go("tracks", {"tracks": []})

    def current_user_saved_albums(self, limit=20, offset=0, market=None):
        return self._go("albums", {"items": [{"album": {"id": "albumid", "name": TITLE}}], "total": 312, "next": None})

    def current_user(self):
        return self._go("me", {"id": "ownerid", "display_name": ARTIST})

    def current_user_playlists(self, limit=50, offset=0):
        mine = {"id": "plmine", "name": TITLE, "owner": {"id": "ownerid"}, "items": {"total": 3}}
        theirs = {"id": "pltheirs", "name": TITLE, "owner": {"id": "someone"}, "items": {"total": 9}}
        return self._go("playlists", {"items": [mine, theirs], "total": 2, "next": None})

    def playlist_items(self, playlist_id, fields=None, limit=50, offset=0, market=None, additional_types=()):
        return self._go(f"items:{playlist_id}", {"items": [], "total": 0, "next": None})

    def current_user_saved_tracks_contains(self, tracks=None):
        return self._go("contains", [True])

    def current_user_followed_artists(self, limit=20, after=None):
        return self._go("follow", {"artists": {"items": [], "total": 0, "next": None}})


@pytest.fixture
def spotify_script(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "spotify_client_id", "cid")
    monkeypatch.setattr(settings, "spotify_client_secret", "sec")
    monkeypatch.setattr(sp, "_rate_limited_until", 0.0)
    monkeypatch.setattr(sp, "RATE_LIMIT_PATH", tmp_path / "spotify_rate_limit.json")
    monkeypatch.setattr(sp, "CACHE_PATH", str(tmp_path / ".spotify_cache"))
    fake = ScriptSpotify()
    monkeypatch.setattr(sp, "get_client", lambda: fake)
    return fake


def run_spotify(capsys) -> tuple[int, str]:
    slept: list[float] = []
    code = spotify_saved_check.main([], pacer=sp.SyncPacer(sleep=slept.append))
    return code, capsys.readouterr().out


def spotify_report(out: str) -> dict:
    return json.loads(out[:out.rindex("}") + 1])


def test_spotify_check_passes_with_counts_statuses_and_no_names_or_ids(spotify_script, capsys):
    Path(sp.CACHE_PATH).write_text(json.dumps(
        {"access_token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH", "scope": sp.AUTH_SCOPES}))

    code, out = run_spotify(capsys)

    assert code == 0 and out.rstrip().endswith("PASS")
    for leak in (TITLE, ARTIST, "sp1id", "albumid", "ownerid", "plmine", "pltheirs", "artistid", "SECRET-"):
        assert leak not in out
    report = spotify_report(out)
    assert report["liked"] == {"status": 200, "total": 2914, "item_key": "item", "has_external_ids": True,
                               "has_isrc": True, "parsed_items": 2}
    assert report["albums"]["album_has_tracks"] is False and report["playlists"]["count_key"] == "items"
    assert (report["playlists"]["owned_in_first_50"], report["playlists"]["followed_in_first_50"]) == (1, 1)
    assert report["owned_items_status"] == report["followed_items_status"] == report["contains_status"] == 200
    assert report["follow_scope"] == "granted" and sp.FOLLOW_SCOPE in report["cached_scopes (requested, not proof of grant)"]
    assert report["calls_sent"] == len(spotify_script.calls) == 9


def test_spotify_check_refuses_to_start_while_rate_limited(spotify_script, capsys, monkeypatch):
    monkeypatch.setattr(sp, "_rate_limited_until", time.monotonic() + 7200)

    code, out = run_spotify(capsys)

    assert code == 1 and "~2 h" in out and "Nothing was sent" in out and spotify_script.calls == []


def test_spotify_check_stops_at_the_first_429_without_retrying(spotify_script, capsys):
    spotify_script.fail["albums"] = spotipy.SpotifyException(429, -1, "slow down", headers={"Retry-After": "3600"})

    code, out = run_spotify(capsys)

    assert code == 1 and "FAIL: rate_limited" in out and "~60 min" in out
    assert spotify_script.calls == ["liked", "tracks", "albums"]
    assert json.loads(sp.RATE_LIMIT_PATH.read_text())["until"] > time.time() + 3000


def test_spotify_check_reports_a_refused_followed_playlist_without_failing(spotify_script, capsys):
    spotify_script.fail["items:pltheirs"] = spotipy.SpotifyException(403, -1, "Forbidden")
    spotify_script.fail["follow"] = spotipy.SpotifyException(403, -1, "Insufficient client scope")

    code, out = run_spotify(capsys)

    report = spotify_report(out)
    assert code == 0 and report["followed_items_status"] == 403 and report["follow_scope"] == "scope_missing"


def test_spotify_check_fails_when_no_liked_song_parses(spotify_script, capsys, monkeypatch):
    monkeypatch.setattr(spotify_script, "current_user_saved_tracks",
                        lambda limit=20, offset=0, market=None: {"items": [{"song": {}}], "total": 1, "next": None})

    code, out = run_spotify(capsys)

    assert code == 1 and "liked_parsed" in out


def test_spotify_check_without_a_connection_exits_2(spotify_script, capsys, monkeypatch):
    monkeypatch.setattr(sp, "get_client", lambda: None)

    assert run_spotify(capsys)[0] == 2
