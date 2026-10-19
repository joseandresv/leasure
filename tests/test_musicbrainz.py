import json

import pytest

from services import musicbrainz


@pytest.fixture
def mb(tmp_path, monkeypatch):
    """The client with a per-test cache file and a clean rate-limiter state."""
    monkeypatch.setattr(musicbrainz, "_cache_path", lambda: tmp_path / "musicbrainz_cache.json")
    monkeypatch.setattr(musicbrainz, "_cache", None)
    monkeypatch.setattr(musicbrainz, "_last_request", 0.0)
    return musicbrainz


def _stub_get(monkeypatch, routes: dict[str, dict]) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def fake_get(path, params):
        calls.append((path, params))
        return routes.get(path)

    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    return calls


def _genres(*pairs):
    return [{"name": name, "count": count} for name, count in pairs]


def _isrc_x_routes():
    return {
        "/isrc/X": {"recordings": [{"id": "r"}]},
        "/recording/r": {"id": "r", "genres": _genres(("jazz", 1))},
    }


async def test_isrc_lookup_returns_count_sorted_recording_genres(mb, monkeypatch):
    calls = _stub_get(monkeypatch, {
        "/isrc/GBDUW0000059": {"recordings": [{"id": "rec-1"}]},
        "/recording/rec-1": {"id": "rec-1", "genres": _genres(("disco", 2), ("french house", 9))},
    })

    genres = await mb.genres_for_track(isrc="gbduw0000059", title="Get Lucky", artist="Daft Punk")

    assert genres == ["french house", "disco"]
    assert [c[0] for c in calls] == ["/isrc/GBDUW0000059", "/recording/rec-1"]


async def test_isrc_resource_is_asked_only_for_artist_credits(mb, monkeypatch):
    """The isrc resource answers HTTP 400 for inc=genres or inc=release-groups."""
    calls = _stub_get(monkeypatch, {
        "/isrc/GBDUW0000059": {"recordings": [{"id": "rec-1"}]},
        "/recording/rec-1": {"id": "rec-1", "genres": _genres(("disco", 2))},
    })

    await mb.genres_for_track(isrc="GBDUW0000059", title="Get Lucky", artist="Daft Punk")

    assert dict(calls)["/isrc/GBDUW0000059"] == {"inc": "artist-credits"}
    assert "genres" in dict(calls)["/recording/rec-1"]["inc"]


async def test_isrc_lookup_skips_a_recording_without_genres(mb, monkeypatch):
    calls = _stub_get(monkeypatch, {
        "/isrc/X": {"recordings": [{"id": "rec-1"}, {"id": "rec-2"}]},
        "/recording/rec-1": {"id": "rec-1"},
        "/recording/rec-2": {"id": "rec-2", "genres": _genres(("soul", 3))},
    })

    assert await mb.genres_for_track(isrc="X", title="T", artist="A") == ["soul"]
    assert [c[0] for c in calls] == ["/isrc/X", "/recording/rec-1", "/recording/rec-2"]


async def test_credited_artist_genres_are_read_from_the_recording_response(mb, monkeypatch):
    calls = _stub_get(monkeypatch, {
        "/isrc/X": {"recordings": [{"id": "rec-1"}]},
        "/recording/rec-1": {
            "id": "rec-1",
            "artist-credit": [{"artist": {"id": "art-1", "genres": _genres(("glam rock", 24), ("art rock", 15))}}],
        },
    })

    assert await mb.genres_for_track(isrc="X", title="T", artist="A") == ["glam rock", "art rock"]
    assert not [c for c in calls if c[0].startswith("/artist/")]


async def test_album_is_part_of_the_non_isrc_cache_key(mb, monkeypatch):
    _stub_get(monkeypatch, {
        "/recording/": {"recordings": [{"id": "rec-1", "title": "Get Lucky", "score": 100}]},
        "/recording/rec-1": {"id": "rec-1", "genres": _genres(("funk", 4))},
    })

    await mb.genres_for_track(isrc=None, title="Get Lucky", artist="Daft Punk", album="Random Access Memories")

    assert "track:daftpunk|getlucky|randomaccessmemories" in mb._load_cache()


async def test_recording_search_rejects_a_hit_below_the_score_threshold(mb, monkeypatch):
    calls = _stub_get(monkeypatch, {
        "/recording/": {"recordings": [{"id": "rec-1", "title": "Get Lucky", "score": 80}]},
        "/artist/": {"artists": []},
    })

    genres = await mb.genres_for_track(isrc=None, title="Get Lucky", artist="Daft Punk")

    assert genres == []
    assert "/recording/rec-1" not in [c[0] for c in calls]


async def test_recording_search_rejects_a_hit_with_a_different_title(mb, monkeypatch):
    _stub_get(monkeypatch, {
        "/recording/": {"recordings": [{"id": "rec-1", "title": "Get Lucky (Daft Punk Remix)", "score": 100}]},
        "/artist/": {"artists": []},
    })

    assert await mb.genres_for_track(isrc=None, title="Get Lucky", artist="Daft Punk") == []


async def test_recording_search_accepted_hit_is_looked_up_for_genres(mb, monkeypatch):
    calls = _stub_get(monkeypatch, {
        "/recording/": {"recordings": [{"id": "rec-1", "title": "Get lucky!", "score": 97}]},
        "/recording/rec-1": {"id": "rec-1", "genres": _genres(("funk", 4))},
    })

    genres = await mb.genres_for_track(isrc=None, title="Get Lucky", artist="Daft Punk, Pharrell Williams")

    assert genres == ["funk"]
    assert calls[0][1]["query"] == 'recording:"Get Lucky" AND artist:"Daft Punk"'


async def test_artist_fallback_uses_genres_not_tags(mb, monkeypatch):
    _stub_get(monkeypatch, {
        "/recording/": {"recordings": []},
        "/artist/": {"artists": [{"id": "art-1", "score": 100}]},
        "/artist/art-1": {"id": "art-1", "tags": _genres(("ignored", 99)), "genres": _genres(("electronic", 5))},
    })

    assert await mb.genres_for_track(isrc=None, title="Get Lucky", artist="Daft Punk") == ["electronic"]


async def test_artist_fallback_rejects_a_low_scoring_artist(mb, monkeypatch):
    _stub_get(monkeypatch, {
        "/recording/": {"recordings": []},
        "/artist/": {"artists": [{"id": "art-1", "score": 55}]},
        "/artist/art-1": {"genres": _genres(("electronic", 5))},
    })

    assert await mb.genres_for_track(isrc=None, title="Get Lucky", artist="Daft Punk") == []


async def test_disk_cache_hit_avoids_a_second_request(mb, monkeypatch):
    calls = _stub_get(monkeypatch, {
        "/isrc/GBDUW0000059": {"recordings": [{"id": "rec-1"}]},
        "/recording/rec-1": {"id": "rec-1", "genres": _genres(("disco", 2))},
    })

    first = await mb.genres_for_track(isrc="GBDUW0000059", title="Get Lucky", artist="Daft Punk")
    monkeypatch.setattr(mb, "_cache", None)  # force a read back from disk
    second = await mb.genres_for_track(isrc="GBDUW0000059", title="Get Lucky", artist="Daft Punk")

    assert first == second == ["disco"]
    assert len(calls) == 2


async def test_a_corrupt_cache_file_is_ignored(mb, monkeypatch):
    mb._cache_path().write_text("{not json", encoding="utf-8")
    _stub_get(monkeypatch, _isrc_x_routes())

    assert await mb.genres_for_track(isrc="X", title="T", artist="A") == ["jazz"]
    assert json.loads(mb._cache_path().read_text(encoding="utf-8"))["isrc:X"]["genres"] == ["jazz"]


async def test_a_stale_cache_entry_is_refetched(mb, monkeypatch):
    mb._cache_path().write_text(
        json.dumps({"isrc:X": {"genres": [], "at": "2020-01-01T00:00:00+00:00"}}), encoding="utf-8"
    )
    calls = _stub_get(monkeypatch, _isrc_x_routes())

    assert await mb.genres_for_track(isrc="X", title="T", artist="A") == ["jazz"]
    assert len(calls) == 2


async def test_a_failing_request_yields_no_genres(mb, monkeypatch):
    async def boom(path, params):
        raise RuntimeError("musicbrainz is down")

    monkeypatch.setattr(mb, "_get", boom)

    assert await mb.genres_for_track(isrc="X", title="T", artist="A") == []


async def test_requests_are_spaced_by_the_rate_limit(mb, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(mb, "_sleep", fake_sleep)

    await mb._throttle()
    await mb._throttle()

    assert len(slept) == 1
    assert 0 < slept[0] <= mb.MIN_INTERVAL


def test_user_agent_identifies_the_app():
    assert musicbrainz.USER_AGENT.startswith("Leasure/")
    assert "https://" in musicbrainz.USER_AGENT


async def test_tagger_joins_the_genre_list(monkeypatch):
    from models import Track
    from services import tagger

    async def fake_genres(*, isrc, title, artist, album=None):
        assert (isrc, title, artist, album) == ("US123", "Get Lucky", "Daft Punk", "RAM")
        return ["disco", "funk"]

    monkeypatch.setattr(musicbrainz, "genres_for_track", fake_genres)
    track = Track(isrc="US123", title="Get Lucky", artist="Daft Punk", album="RAM")

    assert await tagger._fetch_genre(track) == "disco, funk"


async def test_tagger_returns_none_when_no_genres_found(monkeypatch):
    from models import Track
    from services import tagger

    async def no_genres(*, isrc, title, artist, album=None):
        return []

    monkeypatch.setattr(musicbrainz, "genres_for_track", no_genres)

    assert await tagger._fetch_genre(Track(title="x", artist="y")) is None


async def test_search_shares_the_throttled_musicbrainz_client(mb, monkeypatch):
    from services import search

    calls = _stub_get(monkeypatch, {
        "/release": {"releases": [{"id": "rel-1", "title": "Discovery", "artist-credit": [{"name": "Daft Punk"}]}]},
    })

    result = await search.search_musicbrainz("discovery", limit=5)

    assert calls == [("/release", {"query": "discovery", "limit": 5})]
    assert result["albums"][0]["id"] == "mb:rel-1"


# --- get_checked: album lookups tell "unavailable" apart from "not found" ---

REAL_GET_CHECKED = musicbrainz.get_checked


@pytest.fixture
def mb_http(monkeypatch):
    """Answers MusicBrainz requests from a list of (status, json) or exceptions; records the requests."""
    import httpx

    answers: list = []
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(musicbrainz.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(musicbrainz, "_sleep", no_sleep)
    monkeypatch.setattr(musicbrainz, "_last_request", 0.0)
    return answers, requests


async def test_get_checked_returns_the_json(mb_http):
    answers, requests = mb_http
    answers.append((200, {"releases": []}))

    assert await REAL_GET_CHECKED("/release/", {"query": "q"}) == {"releases": []}
    assert requests[0].headers["user-agent"] == musicbrainz.USER_AGENT
    assert requests[0].url.params["fmt"] == "json"


async def test_get_checked_is_none_for_a_404(mb_http):
    mb_http[0].append((404, {"error": "Not Found"}))

    assert await REAL_GET_CHECKED("/release/x", {}) is None


@pytest.mark.parametrize("answers, status", [
    ([(429, {})], 429), ([(503, {}), (503, {})], 503), ([(500, {})], 500), ([(400, {})], 400)])
async def test_get_checked_raises_when_musicbrainz_cannot_answer(mb_http, answers, status):
    mb_http[0].extend(answers)

    with pytest.raises(musicbrainz.MusicBrainzUnavailable) as error:
        await REAL_GET_CHECKED("/release/", {})

    assert error.value.status == status and len(mb_http[1]) == len(answers)


async def test_get_checked_retries_a_503_once(mb_http):
    mb_http[0].extend([(503, {}), (200, {"ok": True})])

    assert await REAL_GET_CHECKED("/release/", {}) == {"ok": True}


async def test_get_checked_raises_on_a_transport_error(mb_http):
    import httpx

    mb_http[0].append(httpx.ConnectError("down"))

    with pytest.raises(musicbrainz.MusicBrainzUnavailable):
        await REAL_GET_CHECKED("/release/", {})


async def test_get_still_returns_none_for_any_failure(mb_http):
    mb_http[0].extend([(429, {}), (404, {})])

    assert await musicbrainz.get("/release/", {}) is None
    assert await musicbrainz.get("/release/x", {}) is None
