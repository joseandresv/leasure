import httpx
import pytest

from services import deezer

REAL_GET_CHECKED = deezer.get_checked


@pytest.fixture
def dz_http(monkeypatch):
    """Answers Deezer requests from a list of (status, json) or exceptions; records requests and sleeps."""
    answers: list = []
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(deezer.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(deezer, "_sleep", fake_sleep)
    monkeypatch.setattr(deezer, "_last_request", 0.0)
    monkeypatch.setattr(deezer, "get_checked", REAL_GET_CHECKED)
    return answers, requests, sleeps


async def test_requests_carry_the_app_user_agent(dz_http):
    answers, requests, _ = dz_http
    answers.append((200, {"data": []}))

    assert await deezer.get_checked("/search/album", {"q": "x"}) == {"data": []}
    assert requests[0].headers["user-agent"].startswith("Leasure/")
    assert str(requests[0].url).startswith("https://api.deezer.com/search/album")


async def test_back_to_back_requests_are_spaced_a_second_apart(dz_http):
    answers, _, sleeps = dz_http
    answers.extend([(200, {}), (200, {})])

    await deezer.get_checked("/album/1", {})
    await deezer.get_checked("/album/2", {})

    assert len(sleeps) == 1 and 0.9 < sleeps[0] <= deezer.MIN_INTERVAL


@pytest.mark.parametrize("code", [4, 700])
async def test_quota_and_busy_errors_wait_and_retry_once(dz_http, code):
    answers, requests, sleeps = dz_http
    answers.extend([(200, {"error": {"code": code, "type": "Exception", "message": "busy"}}), (200, {"id": 1})])

    assert await deezer.get_checked("/album/1", {}) == {"id": 1}
    assert len(requests) == 2 and deezer.RETRY_AFTER in sleeps


async def test_a_second_busy_answer_is_unavailable(dz_http):
    dz_http[0].extend([(200, {"error": {"code": 4}}), (200, {"error": {"code": 4}})])

    with pytest.raises(deezer.DeezerUnavailable, match="error 4"):
        await deezer.get_checked("/album/1", {})


async def test_no_data_is_an_empty_answer(dz_http):
    dz_http[0].append((200, {"error": {"code": 800, "type": "DataException", "message": "no data"}}))

    assert await deezer.get_checked("/album/1", {}) is None


@pytest.mark.parametrize("answer", [(200, {"error": {"code": 100}}), (500, {}),
                                    httpx.ConnectError("down")], ids=["other_code", "http_500", "transport"])
async def test_other_failures_are_unavailable(dz_http, answer):
    dz_http[0].append(answer)

    with pytest.raises(deezer.DeezerUnavailable):
        await deezer.get_checked("/album/1", {})


async def test_search_strips_quotes_from_the_names(dz_http):
    answers, requests, _ = dz_http
    answers.append((200, {"data": [{"id": 1}]}))

    assert await deezer.search_albums('The "Killers"', 'Hot "Fuss"') == [{"id": 1}]
    assert requests[0].url.params["q"] == 'artist:"The Killers" album:"Hot Fuss"'
