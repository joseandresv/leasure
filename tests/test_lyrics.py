import pytest

from services import lyrics

SYNCED = "[00:12.00]We're up all night to get lucky\n"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, get_payload, search_payload, headers=None):
        self._calls = calls
        self._get_payload = get_payload
        self._search_payload = search_payload
        self._headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None, timeout=None):
        self._calls.append({"url": url, "params": params, "headers": self._headers})
        payload = self._get_payload if url.endswith("/get") else self._search_payload
        if payload is None:
            return _FakeResponse({}, status_code=404)
        return _FakeResponse(payload)


@pytest.fixture
def lrclib(monkeypatch):
    """Stub lrclib.net; returns the list of requests the module made."""
    calls = []

    def _install(get_payload=None, search_payload=None):
        class _FakeHttpx:
            @staticmethod
            def AsyncClient(**kwargs):  # noqa: N802 — mirrors httpx's name
                return _FakeClient(calls, get_payload, search_payload, **kwargs)

        monkeypatch.setattr(lyrics, "httpx", _FakeHttpx)
        return calls

    return _install


async def test_get_sends_user_agent_and_the_primary_artist(lrclib):
    calls = lrclib(get_payload={"syncedLyrics": SYNCED})

    result = await lyrics.fetch_lrc("Get Lucky", "Daft Punk, Pharrell Williams", duration_s=369)

    assert result == SYNCED
    assert calls[0]["params"]["artist_name"] == "Daft Punk"
    assert calls[0]["headers"]["User-Agent"] == lyrics.USER_AGENT


async def test_search_accepts_a_hit_within_the_duration_tolerance(lrclib):
    lrclib(get_payload=None, search_payload=[
        {"trackName": "Get Lucky!", "artistName": "Daft Punk feat. Pharrell", "duration": 367,
         "syncedLyrics": SYNCED},
    ])

    assert await lyrics.fetch_lrc("Get Lucky", "Daft Punk", duration_s=369) == SYNCED


async def test_search_rejects_a_hit_with_a_different_duration(lrclib):
    lrclib(get_payload=None, search_payload=[
        {"trackName": "Get Lucky", "artistName": "Daft Punk", "duration": 240, "syncedLyrics": SYNCED},
    ])

    assert await lyrics.fetch_lrc("Get Lucky", "Daft Punk", duration_s=369) is None


async def test_search_rejects_a_hit_with_a_different_title(lrclib):
    lrclib(get_payload=None, search_payload=[
        {"trackName": "Lose Yourself to Dance", "artistName": "Daft Punk", "duration": 369,
         "syncedLyrics": SYNCED},
    ])

    assert await lyrics.fetch_lrc("Get Lucky", "Daft Punk", duration_s=369) is None


async def test_search_rejects_a_hit_by_another_artist(lrclib):
    lrclib(get_payload=None, search_payload=[
        {"trackName": "Get Lucky", "artistName": "Karaoke Allstars", "duration": 369,
         "syncedLyrics": SYNCED},
    ])

    assert await lyrics.fetch_lrc("Get Lucky", "Daft Punk", duration_s=369) is None


async def test_plain_lyrics_are_never_returned(lrclib):
    lrclib(
        get_payload={"plainLyrics": "We're up all night to get lucky"},
        search_payload=[{"trackName": "Get Lucky", "artistName": "Daft Punk", "duration": 369,
                         "plainLyrics": "We're up all night to get lucky"}],
    )

    assert await lyrics.fetch_lrc("Get Lucky", "Daft Punk", duration_s=369) is None


async def test_save_lrc_writes_nothing_when_only_plain_lyrics_exist(lrclib, tmp_path):
    lrclib(get_payload={"plainLyrics": "We're up all night to get lucky"}, search_payload=[])

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"")
    await lyrics.save_lrc(audio, title="Get Lucky", artist="Daft Punk", duration_ms=369000)

    assert not audio.with_suffix(".lrc").exists()
