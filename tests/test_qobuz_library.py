"""QobuzClient favorites / playlists / album reads against synthetic answers (httpx.MockTransport, no network)."""
import json
from urllib.parse import parse_qs

import httpx
import pytest

from services import qobuz_client as qc
from tests.test_qobuz_client import S_BERLIN, Routes, cache_payload, fixture, make_client, reply

COVER = "https://static.qobuz.com/images/covers/ab/cd/abcd_600.jpg"


def fav_track(n: int, **extra) -> dict:
    return {"id": 5000 + n, "title": f"Song {n}", "version": None, "duration": 200, "isrc": "USSM15900113",
            "performer": {"name": "Artist"}, "streamable": True, "maximum_bit_depth": 24,
            "album": {"id": f"alb{n}", "title": "Album", "artist": {"name": "Artist"},
                      "image": {"large": COVER}}, **extra}


def favorites_answer(kind: str, items: list, total: int | None = None, offset: int = 0) -> dict:
    return {kind: {"items": items, "total": len(items) if total is None else total, "limit": 500, "offset": offset}}


def by_offset(kind: str, pages: dict[int, list], total: int):
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json=favorites_answer(kind, pages.get(offset, []), total, offset))
    return handler


def signed_cache(**overrides) -> dict:
    return cache_payload(secret=S_BERLIN, **overrides)


def test_favorite_tracks_parse_full_title_album_and_favorited_at(tmp_path):
    item = fav_track(1, version="Live", favorited_at=1760000000)
    api = Routes({"favorite/getUserFavorites": [reply(200, favorites_answer("tracks", [item, {"id": None}]))]})

    page = make_client(tmp_path, api).favorites("tracks", limit=5)

    (track,) = page.items
    assert (track.id, track.full_title, track.album_id, track.favorited_at) == ("5001", "Song 1 (Live)", "alb1", 1760000000)
    assert track.cover_url == COVER and page.raw_count == 2 and page.total == 2
    assert api.requests[0].url.params["type"] == "tracks" and api.requests[0].url.params["limit"] == "5"


def test_favorite_albums_and_artists_parse(tmp_path):
    album = {"id": "0886443927087", "title": "Random Access Memories", "artist": {"name": "Daft Punk"},
             "tracks_count": 13, "release_date_original": "2013-05-17", "maximum_bit_depth": 24,
             "maximum_sampling_rate": 88.2, "image": {"large": "http://static.qobuz.com/images/x.jpg"}}
    artist = {"id": 36819, "name": "Daft Punk", "albums_count": 120, "image": None}
    api = Routes({"favorite/getUserFavorites": [reply(200, favorites_answer("albums", [album])),
                                                reply(200, favorites_answer("artists", [artist]))]})
    client = make_client(tmp_path, api)

    (got_album,) = client.favorites("albums").items
    (got_artist,) = client.favorites("artists").items

    assert (got_album.id, got_album.year, got_album.tracks_count, got_album.cover_url) == ("0886443927087", 2013, 13, None)
    assert (got_artist.id, got_artist.name, got_artist.albums_count) == ("36819", "Daft Punk", 120)


def test_an_unknown_answer_shape_is_a_shape_error(tmp_path):
    api = Routes({"favorite/getUserFavorites": [reply(200, {"tracks": ["not", "a", "page"]})]})

    with pytest.raises(qc.QobuzShapeError):
        make_client(tmp_path, api).favorites("tracks")


def test_paging_advances_by_items_received_and_stops_at_total(tmp_path):
    pages = {0: [fav_track(i) for i in range(3)], 3: [fav_track(i) for i in range(3, 5)]}
    api = Routes({"favorite/getUserFavorites": [by_offset("tracks", pages, total=5)]})

    got = list(make_client(tmp_path, api).iter_favorites("tracks"))

    assert [p.offset for p in got] == [0, 3] and sum(len(p.items) for p in got) == 5
    assert len(api.requests) == 2


def test_paging_stops_on_an_empty_page(tmp_path):
    api = Routes({"favorite/getUserFavorites": [by_offset("tracks", {0: [fav_track(1)]}, total=10)]})

    got = list(make_client(tmp_path, api).iter_favorites("tracks"))

    assert [p.raw_count for p in got] == [1, 0]


def test_paging_stops_at_the_page_cap(tmp_path):
    same = favorites_answer("tracks", [fav_track(1)], total=1000)
    api = Routes({"favorite/getUserFavorites": [reply(200, same)]})

    got = list(make_client(tmp_path, api).iter_favorites("tracks", page_cap=3))

    assert len(got) == 3 and len(api.requests) == 3


def test_a_refused_page_size_steps_down_to_100_and_stays_there(tmp_path):
    def handler(request):
        if request.url.params["limit"] == "500":
            return httpx.Response(400, json={"message": "Invalid argument: limit"})
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=favorites_answer("tracks", [fav_track(offset)], total=2, offset=offset))
    api = Routes({"favorite/getUserFavorites": [handler]})
    client = make_client(tmp_path, api)

    got = list(client.iter_favorites("tracks"))

    assert len(got) == 2 and client.library_page_size == qc.LIBRARY_FALLBACK_PAGE_SIZE
    assert [r.url.params["limit"] for r in api.requests if "request_sig" not in r.url.params] == ["500", "100", "100"]


def test_unsigned_refusal_is_retried_once_signed_with_the_proven_secret_and_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(qc.time, "time", lambda: 1700000000)
    api = Routes({"favorite/getUserFavorites": [reply(400, {"message": "Invalid request signature"}),
                                                reply(200, favorites_answer("tracks", [fav_track(1)]))]})
    client = make_client(tmp_path, api, cache=signed_cache())

    client.favorites("tracks")
    client.favorites("tracks")

    unsigned, signed, again = api.requests
    assert "request_sig" not in unsigned.url.params
    assert signed.url.params["request_ts"] == "1700000000"
    assert signed.url.params["request_sig"] == qc.library_signature("favorite/getUserFavorites", "1700000000", S_BERLIN)
    assert "request_sig" in again.url.params
    assert client.library_attempts == (("signed", 200),)
    assert json.loads((tmp_path / "qobuz_app.json").read_text())["favorites_signed"] is True


def test_library_signature_is_qobuz_dl_md5():
    assert qc.library_signature("favorite/getUserFavorites", "1700000000", "s3cr3t") == \
        qc.hashlib.md5(b"favoritegetUserFavorites1700000000s3cr3t").hexdigest()


def test_an_unsigned_401_costs_no_login(tmp_path):
    api = Routes({"favorite/getUserFavorites": [reply(401), reply(200, favorites_answer("tracks", []))],
                  "user/login": [reply(200, fixture("user_login.json"))]})

    make_client(tmp_path, api, cache=signed_cache()).favorites("tracks")

    assert api.to("user/login") == [] and len(api.requests) == 2


def test_a_signed_401_confirms_the_sign_in_once_per_client(tmp_path):
    api = Routes({"favorite/getUserFavorites": [reply(401)], "playlist/getUserPlaylists": [reply(401)],
                  "user/login": [reply(401)]})
    client = make_client(tmp_path, api, cache=signed_cache())

    with pytest.raises(qc.QobuzAuthError):
        client.favorites("tracks")
    with pytest.raises(qc.QobuzAuthError):
        client.user_playlists()

    assert len(api.to("user/login")) == len(qc.LOGIN_SHAPES)
    signed = [r for r in api.requests if "request_sig" in r.url.params]
    assert len(signed) == 2


def test_without_a_proven_secret_only_the_unsigned_call_is_made(tmp_path):
    api = Routes({"favorite/getUserFavorites": [reply(400, {"message": "Invalid argument"})]})

    client = make_client(tmp_path, api)

    with pytest.raises(qc.QobuzError) as raised:
        client.favorites("tracks")

    assert raised.value.status == 400 and client.library_attempts == (("unsigned", 400),)


def test_a_403_on_a_list_endpoint_is_forbidden_when_the_login_still_works(tmp_path):
    api = Routes({"playlist/getUserPlaylists": [reply(403)], "user/login": [reply(200, fixture("user_login.json"))]})

    with pytest.raises(qc.QobuzForbiddenError):
        make_client(tmp_path, api, cache=signed_cache()).user_playlists()


def test_favorite_ids_are_strings_per_type(tmp_path):
    api = Routes({"favorite/getUserFavoriteIds": [reply(200, {"tracks": [1, 2, None], "albums": ["a1"],
                                                              "artists": [], "articles": [9]})]})

    ids = make_client(tmp_path, api).favorite_ids()

    assert ids == {"tracks": ["1", "2"], "albums": ["a1"], "artists": []}
    assert api.requests[0].url.params["user_id"] == "1234567"


def test_user_playlists_and_filter(tmp_path):
    playlist = {"id": 42, "name": "Mine", "tracks_count": 12, "owner": {"id": 1234567, "name": "me"},
                "updated_at": 1760000000, "is_public": False, "is_collaborative": False}
    api = Routes({"playlist/getUserPlaylists": [reply(200, {"playlists": {"items": [playlist], "total": 1}})]})

    (got,) = make_client(tmp_path, api).user_playlists(limit=5, owner_filter="owner").items

    assert (got.id, got.tracks_count, got.owner_id, got.updated_at) == ("42", 12, "1234567", 1760000000)
    assert parse_qs(api.requests[0].url.query.decode()) == {"limit": ["5"], "offset": ["0"], "filter": ["owner"]}


def test_playlist_get_pages_its_tracks(tmp_path):
    def handler(request):
        offset = int(request.url.params["offset"])
        assert request.url.params["extra"] == "tracks"
        return httpx.Response(200, json={"id": 42, "name": "Mine", "tracks_count": 3,
                                         "tracks": {"items": [fav_track(offset + i) for i in range(2 if offset == 0 else 1)],
                                                    "total": 3}})
    api = Routes({"playlist/get": [handler]})

    pages = list(make_client(tmp_path, api).iter_playlist_tracks("42"))

    assert [len(p.items) for p in pages] == [2, 1] and pages[1].items[0].id == "5002"


def album_answer(tracks: list, total: int) -> dict:
    return {"id": "alb1", "title": "Album", "artist": {"name": "Artist"}, "tracks_count": total,
            "image": {"large": COVER}, "tracks": {"items": tracks, "total": total}}


def test_album_tracks_get_the_parent_album_and_page_while_total_is_larger(tmp_path):
    def handler(request):
        offset = int(request.url.params["offset"])
        items = [{"id": 100 + offset + i, "title": f"T{offset + i}", "performer": {"name": "Artist"}}
                 for i in range(2 if offset == 0 else 1)]
        return httpx.Response(200, json=album_answer(items, total=3))
    api = Routes({"album/get": [handler]})

    album, tracks = make_client(tmp_path, api).get_album("alb1")

    assert album.id == "alb1" and [t.id for t in tracks] == ["100", "101", "102"]
    assert {t.album_id for t in tracks} == {"alb1"} and tracks[0].cover_url == COVER


def test_album_paging_ends_when_qobuz_ignores_the_offset(tmp_path):
    same = album_answer([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}], total=5)
    api = Routes({"album/get": [reply(200, same)]})

    _, tracks = make_client(tmp_path, api).get_album("alb1")

    assert [t.id for t in tracks] == ["1", "2"] and len(api.requests) == 2
