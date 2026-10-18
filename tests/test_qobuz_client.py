"""services/qobuz_client.py against synthetic Qobuz answers (httpx.MockTransport, no network)."""
import base64
import json
import logging
import threading
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from services import qobuz_client as qc
from services.qobuz_client import QobuzClient, Throttle

FIXTURES = Path(__file__).parent / "fixtures" / "qobuz"
USER_ID = "1234567"
TOKEN = "tok_FAKE_user_auth_token_0123456789abcdef"
APP_ID = "123456789"
APP_SECRET = "fedcba9876543210fedcba9876543210"
S_BERLIN = "0123456789abcdef0123456789abcdef"
S_LONDON = "abcdef0123456789abcdef0123456789"
JUNK = ("Zm9vYmFyYmF6" * 4)[:44]
CDN_URL = "https://streaming-qobuz-std.akamaized.net/file?uid=1&eid=9&fmt=27&hmac=deadbeefcafe0123"
CDN_QUERY = "uid=1&eid=9&fmt=27&hmac=deadbeefcafe0123"
LOGIN_HTML = '<html><script src="/resources/8.2.0-b034/bundle.js"></script></html>'


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def zone_parts(secret_text: str) -> tuple[str, str, str]:
    joined = base64.standard_b64encode(secret_text.encode()).decode() + JUNK
    return joined[:20], joined[20:60], joined[60:]


def make_bundle(app_id: str = APP_ID,
                zones=(("berlin", S_BERLIN), ("london", S_LONDON), ("abidjan", "not-a-hex-secret-not-a-hex-secre"))) -> str:
    out = [f'var c={{production:{{api:{{appId:"{app_id}",appSecret:"{APP_SECRET}"}}}}}};']
    parts = {tz: zone_parts(secret) for tz, secret in zones}
    for letter, (tz, _) in zip("abc", zones, strict=False):
        out.append(f'{letter}.initialSeed("{parts[tz][0]}",window.utimezone.{tz});')
    for tz, _ in zones:
        out.append(f'{{offset:"+1",name:"Europe/{tz.capitalize()}",info:"{parts[tz][1]}",extras:"{parts[tz][2]}"}},')
    return "".join(out)


def reply(status: int = 200, payload=None, headers=None):
    return lambda request: httpx.Response(status, json=payload, headers=headers)


class Routes:
    """Per-path response queues; the last response of a queue repeats."""

    def __init__(self, routes: dict | None = None):
        self.routes = {k: list(v) for k, v in (routes or {}).items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path.removeprefix("/api.json/0.2/")
        queue = self.routes[key]
        handler = queue.pop(0) if len(queue) > 1 else queue[0]
        return handler(request)

    def to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(path)]


def assert_no_credentials(request: httpx.Request) -> None:
    assert "x-user-auth-token" not in request.headers
    assert "x-app-id" not in request.headers
    assert TOKEN not in str(request.url)


class FakeWeb:
    def __init__(self, bundle: str | None = None, delay: float = 0.0):
        self.bundle = bundle or make_bundle()
        self.delay = delay
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert_no_credentials(request)
        self.requests.append(request)
        time.sleep(self.delay)
        if request.url.path == "/login":
            return httpx.Response(200, text=LOGIN_HTML)
        assert request.url.path == "/resources/8.2.0-b034/bundle.js"
        return httpx.Response(200, text=self.bundle)

    @property
    def login_hits(self) -> int:
        return sum(r.url.path == "/login" for r in self.requests)


class FakeCdn:
    def __init__(self, *handlers):
        self.handlers = list(handlers) or [lambda r: httpx.Response(200, content=b"fLaC" + bytes(96))]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert_no_credentials(request)
        self.requests.append(request)
        handler = self.handlers.pop(0) if len(self.handlers) > 1 else self.handlers[0]
        return handler(request)


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"fLaC"
        raise httpx.ReadError("connection reset")


def cache_payload(**overrides) -> dict:
    data = {"app_id": APP_ID, "secrets": [S_LONDON, S_BERLIN], "secret": None,
            "bundle_version": "8.2.0-b034", "login_shape": None, "fetched_at": "2026-10-18T00:00:00+00:00"}
    data.update(overrides)
    return data


def make_client(tmp_path, api=None, *, web=None, cdn=None, cache=True, **kwargs) -> QobuzClient:
    path = tmp_path / "qobuz_app.json"
    if cache is True:
        path.write_text(json.dumps(cache_payload()))
    elif isinstance(cache, dict):
        path.write_text(json.dumps(cache))
    elif isinstance(cache, str):
        path.write_text(cache)
    kwargs.setdefault("throttle", Throttle(1e9, sleep=lambda s: None))
    kwargs.setdefault("retry_sleep", lambda s: None)
    return QobuzClient(USER_ID, TOKEN, app_cache_path=path,
                       transport=httpx.MockTransport(api or Routes()),
                       web_transport=httpx.MockTransport(web or FakeWeb()),
                       cdn_transport=httpx.MockTransport(cdn or FakeCdn()), **kwargs)


def file_url_payload(**overrides) -> dict:
    data = {"track_id": 9, "duration": 200, "url": CDN_URL, "format_id": 27, "mime_type": "audio/flac",
            "sampling_rate": 96, "bit_depth": 24}
    data.update(overrides)
    return data


@pytest.fixture(autouse=True)
def fresh_client_state():
    qc._reset_state_for_tests()
    yield
    qc._reset_state_for_tests()


def test_request_signature_is_pinned_md5():
    assert qc.request_signature("12345", 27, "1700000000", "s3cr3t") == "528f6b68371a165ca0174ddb53a7c05b"


def test_parse_bundle_url_gives_path_and_version():
    assert qc.parse_bundle_url(LOGIN_HTML) == ("/resources/8.2.0-b034/bundle.js", "8.2.0-b034")


def test_parse_app_credentials_decodes_secrets_in_try_order():
    app_id, secrets = qc.parse_app_credentials(make_bundle())
    assert app_id == APP_ID
    assert secrets == [S_LONDON, S_BERLIN]
    assert all(len(s) == 32 and int(s, 16) >= 0 for s in secrets)


@pytest.mark.parametrize("text", ["<html>no bundle</html>", ""])
def test_parse_bundle_url_missing_raises_app_error(text):
    with pytest.raises(qc.QobuzAppError):
        qc.parse_bundle_url(text)


@pytest.mark.parametrize("bundle", ["no app id here", make_bundle().split("a.initialSeed")[0]])
def test_parse_app_credentials_missing_patterns_raise_app_error(bundle):
    with pytest.raises(qc.QobuzAppError):
        qc.parse_app_credentials(bundle)


def test_missing_cache_scrapes_once_and_writes_cache_without_token(tmp_path):
    written = {}
    web = FakeWeb()
    client = make_client(tmp_path, web=web, cache=None, write_text=lambda p, t: written.update({p: t}))
    app = client.app_credentials()
    assert (app.app_id, app.secrets, app.bundle_version) == (APP_ID, (S_LONDON, S_BERLIN), "8.2.0-b034")
    assert len(web.requests) == 2
    (text,) = written.values()
    assert json.loads(text)["app_id"] == APP_ID
    assert TOKEN not in text


def test_corrupt_cache_rescrapes(tmp_path):
    web = FakeWeb()
    client = make_client(tmp_path, web=web, cache="{not json")
    assert client.app_credentials().app_id == APP_ID
    assert web.login_hits == 1


def test_concurrent_clients_scrape_once(tmp_path):
    web = FakeWeb(delay=0.2)
    clients = [make_client(tmp_path, web=web, cache=None) for _ in range(2)]
    results = []
    threads = [threading.Thread(target=lambda c=c: results.append(c.app_credentials().app_id)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [APP_ID, APP_ID]
    assert web.login_hits == 1


def test_forced_rescrape_inside_window_makes_no_request(tmp_path):
    web = FakeWeb()
    client = make_client(tmp_path, web=web, cache=None)
    client.app_credentials()
    with pytest.raises(qc.QobuzAppError):
        client.app_credentials(refresh=True)
    assert len(web.requests) == 2


def test_login_header_shape_keeps_token_out_of_url(tmp_path):
    api = Routes({"user/login": [reply(200, fixture("user_login.json"))]})
    user = make_client(tmp_path, api).login()
    (req,) = api.requests
    assert req.method == "POST"
    assert req.headers["x-user-auth-token"] == TOKEN
    assert req.headers["x-app-id"] == APP_ID
    assert TOKEN not in str(req.url)
    assert parse_qs(req.content.decode()) == {"extra": ["partner"]}
    assert user.user_id == "1234567"


def test_login_falls_through_form_then_query_and_remembers_shape(tmp_path):
    api = Routes({"user/login": [reply(400), reply(401), reply(200, fixture("user_login.json"))]})
    client = make_client(tmp_path, api)
    client.login()
    header, form, query = api.requests
    assert form.method == "POST" and "x-user-auth-token" not in form.headers
    assert parse_qs(form.content.decode())["user_auth_token"] == [TOKEN]
    assert query.method == "GET"
    assert dict(query.url.params) == {"user_id": USER_ID, "user_auth_token": TOKEN, "app_id": APP_ID}
    assert json.loads((tmp_path / "qobuz_app.json").read_text())["login_shape"] == "query"


def test_cached_login_shape_is_tried_first(tmp_path):
    api = Routes({"user/login": [reply(200, fixture("user_login.json"))]})
    make_client(tmp_path, api, cache=cache_payload(login_shape="query")).login()
    (req,) = api.requests
    assert req.method == "GET" and req.url.params["user_auth_token"] == TOKEN


def test_login_all_401_rescrapes_once_retries_once_then_auth_error(tmp_path):
    api = Routes({"user/login": [reply(401)]})
    web = FakeWeb(bundle=make_bundle(app_id="987654321"))
    with pytest.raises(qc.QobuzAuthError):
        make_client(tmp_path, api, web=web).login()
    assert web.login_hits == 1
    assert len(api.requests) == 6
    assert {r.headers["x-app-id"] for r in api.requests[3:]} == {"987654321"}


def test_login_mixed_400_401_is_plain_error(tmp_path):
    api = Routes({"user/login": [reply(400), reply(401), reply(400)]})
    with pytest.raises(qc.QobuzError) as exc:
        make_client(tmp_path, api).login()
    assert not isinstance(exc.value, qc.QobuzAuthError)
    assert str(exc.value) == "Qobuz did not accept the sign-in request."


def test_login_parses_plan_fields(tmp_path):
    api = Routes({"user/login": [reply(200, fixture("user_login.json"))]})
    user = make_client(tmp_path, api).login()
    assert (user.plan, user.lossless, user.hires, user.country) == ("Studio", True, True, "ES")
    assert user.parameters["short_label"] == "Studio"
    assert "nested" not in user.parameters


def test_login_without_parameters_leaves_lossless_unknown(tmp_path):
    payload = {"user": {"id": 1, "credential": {"description": "Qobuz Studio"}}}
    user = make_client(tmp_path, Routes({"user/login": [reply(200, payload)]})).login()
    assert (user.lossless, user.hires, user.plan) == (None, None, "Qobuz Studio")


def test_login_switches_to_issued_token(tmp_path):
    new_token = "tok_ROTATED_0123456789abcdef0123"
    payload = {**fixture("user_login.json"), "user_auth_token": new_token}
    api = Routes({"user/login": [reply(200, payload)], "track/search": [reply(200, fixture("search.json"))]})
    client = make_client(tmp_path, api)
    user = client.login()
    assert user.issued_token == new_token and client.rotated_token == new_token
    assert new_token not in repr(user)
    assert "user_auth_token" not in json.dumps(user.raw)
    client.search_tracks("x")
    assert api.requests[-1].headers["x-user-auth-token"] == new_token


def test_search_parses_tracks(tmp_path):
    api = Routes({"track/search": [reply(200, fixture("search.json"))]})
    first, second = make_client(tmp_path, api).search_tracks("Miles Davis So What")
    assert first.full_title == "So What (2009 Remaster)"
    assert first.isrc == "USSM15900113"
    assert first.max_sampling_rate == 192.0 and second.max_sampling_rate == 44.1
    assert first.cover_url.endswith("_600.jpg")
    assert second.performer == "Miles Davis Quintet"
    assert second.album_id == "77" and second.cover_url is None
    assert second.full_title == "So What (Live)"


@pytest.mark.parametrize("url", [
    "http://static.qobuz.com/images/covers/a.jpg",
    "https://evil.example/images/covers/a.jpg",
    "https://static.qobuz.com/images/../x",
    "https://static.qobuz.com/images/%2e%2e/x",
    "https://static.qobuz.com:8443/images/a.jpg",
    "https://user@static.qobuz.com/images/a.jpg",
    "https://static.qobuz.com/other/a.jpg",
])
def test_cover_url_rejects_unsafe_urls(url):
    assert qc._safe_cover_url(url) is None


def test_cover_url_keeps_qobuz_image_host():
    url = "https://static.qobuz.com/images/covers/87/70/a_600.jpg"
    assert qc._safe_cover_url(url) == url


def test_find_by_isrc_keeps_exact_matches_only(tmp_path):
    api = Routes({"track/search": [reply(200, fixture("search.json"))]})
    tracks = make_client(tmp_path, api).find_by_isrc("ussm15900113")
    assert [t.id for t in tracks] == ["52219581"]
    assert api.requests[0].url.params["query"] == "USSM15900113"


def test_get_file_url_signs_request(tmp_path):
    api = Routes({"track/getFileUrl": [reply(200, file_url_payload())]})
    stream = make_client(tmp_path, api).get_file_url("9", 27)
    params = api.requests[0].url.params
    assert (params["track_id"], params["format_id"], params["intent"]) == ("9", "27", "stream")
    assert params["request_sig"] == qc.request_signature("9", 27, params["request_ts"], S_LONDON)
    assert (stream.format_id, stream.bit_depth, stream.sampling_rate) == (27, 24, 96.0)


def test_get_file_url_rotates_secret_and_persists_working_one(tmp_path):
    bad_sig = reply(400, {"status": "error", "code": 400, "message": "Invalid Request Signature parameter (request_sig)"})
    api = Routes({"track/getFileUrl": [bad_sig, reply(200, file_url_payload())]})
    make_client(tmp_path, api).get_file_url("9", 27)
    second = api.requests[1].url.params
    assert second["request_sig"] == qc.request_signature("9", 27, second["request_ts"], S_BERLIN)
    assert json.loads((tmp_path / "qobuz_app.json").read_text())["secret"] == S_BERLIN


def test_get_file_url_other_400_is_plain_error_without_rotation(tmp_path):
    api = Routes({"track/getFileUrl": [reply(400, {"message": "Invalid track_id"})]})
    with pytest.raises(qc.QobuzError) as exc:
        make_client(tmp_path, api).get_file_url("9", 27)
    assert type(exc.value) is qc.QobuzError
    assert str(exc.value) == "Invalid track_id"
    assert len(api.requests) == 1


def test_get_file_url_app_id_400_rescrapes_once_then_app_error(tmp_path):
    api = Routes({"track/getFileUrl": [reply(400, {"message": "Invalid or missing app_id parameter"})]})
    web = FakeWeb()
    with pytest.raises(qc.QobuzAppError):
        make_client(tmp_path, api, web=web).get_file_url("9", 27)
    assert web.login_hits == 1
    assert len(api.requests) == 1


def test_get_file_url_all_secrets_rejected_is_app_error(tmp_path):
    bad_sig = reply(400, {"message": "Invalid Request Signature parameter (request_sig)"})
    api = Routes({"track/getFileUrl": [bad_sig]})
    web = FakeWeb()
    with pytest.raises(qc.QobuzAppError):
        make_client(tmp_path, api, web=web).get_file_url("9", 27)
    assert len(api.requests) == 2 and web.login_hits == 1


def test_get_file_url_401_with_working_login_is_restricted_and_not_persisted(tmp_path):
    api = Routes({"track/getFileUrl": [reply(401)], "user/login": [reply(200, fixture("user_login.json"))]})
    with pytest.raises(qc.QobuzNotStreamableError) as exc:
        make_client(tmp_path, api).get_file_url("9", 27)
    assert exc.value.reason == "restricted"
    assert json.loads((tmp_path / "qobuz_app.json").read_text())["secret"] is None


def test_get_file_url_401_confirmed_by_login_is_auth_error(tmp_path):
    api = Routes({"track/getFileUrl": [reply(401)], "user/login": [reply(401)]})
    with pytest.raises(qc.QobuzAuthError):
        make_client(tmp_path, api).get_file_url("9", 27)


def test_search_403_with_working_login_is_forbidden(tmp_path):
    api = Routes({"track/search": [reply(403)], "user/login": [reply(200, fixture("user_login.json"))]})
    with pytest.raises(qc.QobuzForbiddenError):
        make_client(tmp_path, api).search_tracks("x")


def test_resolve_stream_steps_down_from_352khz_to_format_7(tmp_path):
    api = Routes({"track/getFileUrl": [reply(200, file_url_payload(sampling_rate=352.8)),
                                       reply(200, file_url_payload(format_id=7, sampling_rate=96))]})
    stream = make_client(tmp_path, api).resolve_stream("9", 27)
    assert [r.url.params["format_id"] for r in api.requests] == ["27", "7"]
    assert stream.format_id == 7


def test_resolve_stream_too_high_after_step_down(tmp_path):
    api = Routes({"track/getFileUrl": [reply(200, file_url_payload(sampling_rate=352800))]})
    with pytest.raises(qc.QobuzNotStreamableError) as exc:
        make_client(tmp_path, api).resolve_stream("9", 27)
    assert exc.value.reason == "too_high"


@pytest.mark.parametrize("overrides,expected,reason", [
    ({"sample": True}, None, "sample"),
    ({"sample": None}, None, "sample"),
    ({"duration": 30}, 200.0, "sample"),
    ({"format_id": "5", "mime_type": "audio/mpeg"}, None, "lossy_only"),
    ({"url": None}, None, "no_url"),
    ({"mime_type": "audio/mp4"}, None, "not_flac"),
])
def test_resolve_stream_rejects(tmp_path, overrides, expected, reason):
    api = Routes({"track/getFileUrl": [reply(200, file_url_payload(**overrides))]})
    with pytest.raises(qc.QobuzNotStreamableError) as exc:
        make_client(tmp_path, api).resolve_stream("9", 27, expected_duration_s=expected)
    assert exc.value.reason == reason


def test_resolve_stream_accepts_restricted_format_6(tmp_path):
    payload = file_url_payload(format_id=6, bit_depth=16, sampling_rate=44.1,
                               restrictions=[{"code": "FormatRestrictedByFormatAvailability"}])
    stream = make_client(tmp_path, Routes({"track/getFileUrl": [reply(200, payload)]})).resolve_stream("9", 27)
    assert stream.format_id == 6
    assert stream.restrictions == ("FormatRestrictedByFormatAvailability",)
    assert "url" not in stream.raw


def file_url_routes(*payloads) -> Routes:
    return Routes({"track/getFileUrl": [reply(200, p) for p in payloads] or [reply(200, file_url_payload())]})


def test_download_writes_file_and_leaves_no_part(tmp_path):
    dest = tmp_path / "source.flac"
    calls = []
    result = make_client(tmp_path, file_url_routes()).download("9", dest, expected_duration_s=200,
                                                                before_body=lambda s, cl: calls.append(cl))
    assert dest.read_bytes() == b"fLaC" + bytes(96)
    assert (result.size, result.content_length, calls) == (100, 100, [100])
    assert not dest.with_name("source.flac.part").exists()


def test_download_content_length_mismatch_fails_cleanly(tmp_path):
    short = FakeCdn(lambda r: httpx.Response(200, headers={"content-length": "500"}, stream=httpx.ByteStream(b"fLaC")))
    dest = tmp_path / "source.flac"
    with pytest.raises(qc.QobuzDownloadError):
        make_client(tmp_path, file_url_routes(), cdn=short).download("9", dest)
    assert list(tmp_path.glob("source.flac*")) == []


def test_download_cdn_403_at_start_resolves_again(tmp_path):
    api = file_url_routes()
    cdn = FakeCdn(lambda r: httpx.Response(403), lambda r: httpx.Response(200, content=b"fLaC"))
    make_client(tmp_path, api, cdn=cdn).download("9", tmp_path / "source.flac")
    assert len(api.to("getFileUrl")) == 2


def test_download_retries_once_after_mid_stream_error(tmp_path):
    cdn = FakeCdn(lambda r: httpx.Response(200, headers={"content-length": "10"}, stream=BrokenStream()),
                  lambda r: httpx.Response(200, content=b"fLaC"))
    calls = []
    make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac",
                                                               before_body=lambda s, cl: calls.append(cl))
    assert calls == [10, 4]


def test_download_two_mid_stream_errors_fail(tmp_path):
    cdn = FakeCdn(lambda r: httpx.Response(200, stream=BrokenStream()))
    with pytest.raises(qc.QobuzDownloadError):
        make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac")
    assert list(tmp_path.glob("source.flac*")) == []


def test_download_steps_down_when_file_exceeds_fat32(tmp_path):
    api = file_url_routes(file_url_payload(), file_url_payload(format_id=7))
    cdn = FakeCdn(lambda r: httpx.Response(200, headers={"content-length": str(qc.FAT32_LIMIT)}),
                  lambda r: httpx.Response(200, content=b"fLaC"))
    result = make_client(tmp_path, api, cdn=cdn).download("9", tmp_path / "source.flac", expected_duration_s=200)
    assert [r.url.params["format_id"] for r in api.to("getFileUrl")] == ["27", "7"]
    assert result.stream.format_id == 7


def test_download_rejects_implausible_length_before_body(tmp_path):
    cdn = FakeCdn(lambda r: httpx.Response(200, headers={"content-length": str(3 * 1024**3)}))
    calls = []
    with pytest.raises(qc.QobuzDownloadError, match="implausibly large"):
        make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac",
                                                                   before_body=lambda s, cl: calls.append(cl))
    assert calls == []


def test_download_aborts_body_past_ceiling_without_length(tmp_path, monkeypatch):
    monkeypatch.setattr(qc, "_size_ceiling", lambda duration: 8)
    cdn = FakeCdn(lambda r: httpx.Response(200, stream=httpx.ByteStream(b"fLaC" * 4)))
    with pytest.raises(qc.QobuzDownloadError):
        make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac")
    assert list(tmp_path.glob("source.flac*")) == []


@pytest.mark.parametrize("location", ["http://streaming-qobuz-std.akamaized.net/file", "https://evil.example/file"])
def test_download_refuses_redirect_off_cdn(tmp_path, location):
    cdn = FakeCdn(lambda r: httpx.Response(302, headers={"location": location}))
    with pytest.raises(qc.QobuzDownloadError, match="unexpected host"):
        make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac")
    assert len(cdn.requests) == 1


def test_download_follows_redirect_within_cdn(tmp_path):
    cdn = FakeCdn(lambda r: httpx.Response(302, headers={"location": "https://edge2.akamaized.net/f"}),
                  lambda r: httpx.Response(200, content=b"fLaC"))
    make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac")
    assert cdn.requests[1].url.host == "edge2.akamaized.net"


def test_download_before_body_exception_propagates_unchanged(tmp_path):
    class Deferred(Exception):
        pass

    def hook(stream, length):
        raise Deferred("cap")

    with pytest.raises(Deferred):
        make_client(tmp_path, file_url_routes()).download("9", tmp_path / "source.flac", before_body=hook)
    assert list(tmp_path.glob("source.flac*")) == []


def test_guard_exception_propagates_before_any_api_call(tmp_path):
    class Changed(Exception):
        pass

    def guard():
        raise Changed

    api = file_url_routes()
    with pytest.raises(Changed):
        make_client(tmp_path, api, guard=guard).download("9", tmp_path / "source.flac")
    assert api.requests == []


def test_parse_retry_after_seconds_and_http_date():
    assert qc.parse_retry_after("30") == 30.0
    now = time.time()
    assert 59 <= qc.parse_retry_after(formatdate(now + 60, usegmt=True), now=now) <= 61
    assert qc.parse_retry_after("soon") is None and qc.parse_retry_after(None) is None


def test_429_carries_retry_after(tmp_path):
    api = Routes({"track/search": [reply(429, headers={"Retry-After": "45"})]})
    with pytest.raises(qc.QobuzRateLimitError) as exc:
        make_client(tmp_path, api).search_tracks("x")
    assert exc.value.retry_after == 45.0


class CountingThrottle(Throttle):
    def __init__(self):
        super().__init__(1e9, sleep=lambda s: None)
        self.calls = 0

    def wait(self):
        self.calls += 1


def test_5xx_retries_once_through_the_throttle(tmp_path):
    throttle, sleeps = CountingThrottle(), []
    api = Routes({"track/search": [reply(503), reply(200, fixture("search.json"))]})
    tracks = make_client(tmp_path, api, throttle=throttle, retry_sleep=sleeps.append).search_tracks("x")
    assert len(tracks) == 2
    assert (throttle.calls, sleeps) == (2, [2])


def test_5xx_twice_is_network_error(tmp_path):
    with pytest.raises(qc.QobuzNetworkError):
        make_client(tmp_path, Routes({"track/search": [reply(503)]})).search_tracks("x")


def raising(exc):
    def handler(request):
        raise exc
    return handler


@pytest.mark.parametrize("exc", [httpx.ConnectError("connect failed to " + CDN_URL),
                                 httpx.TooManyRedirects("too many " + CDN_URL)])
def test_api_transport_errors_become_network_error_without_url(tmp_path, exc):
    api = Routes({"track/getFileUrl": [raising(exc)]})
    with pytest.raises(qc.QobuzNetworkError) as info:
        make_client(tmp_path, api).get_file_url("9", 27)
    assert "http" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize("exc", [httpx.InvalidURL("bad " + CDN_URL), httpx.ConnectError("refused " + CDN_URL)])
def test_cdn_transport_errors_become_download_error_without_url(tmp_path, exc):
    cdn = FakeCdn(raising(exc))
    with pytest.raises(qc.QobuzDownloadError) as info:
        make_client(tmp_path, file_url_routes(), cdn=cdn).download("9", tmp_path / "source.flac")
    assert "http" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_throttle_spaces_calls_at_rate():
    now, sleeps = [0.0], []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    throttle = Throttle(20, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        throttle.wait()
    assert sleeps == [3.0, 3.0]


def test_throttle_serialises_threads():
    lock, now, sleeps = threading.Lock(), [0.0], []

    def sleep(s):
        with lock:
            sleeps.append(s)
            now[0] += s

    throttle = Throttle(20, clock=lambda: now[0], sleep=sleep)
    threads = [threading.Thread(target=throttle.wait) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sleeps == [3.0] * 5
    assert now[0] == 15.0


def test_secrets_never_reach_logs_exceptions_or_reprs(tmp_path, caplog):
    qc.install_log_redaction()
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    try:
        api = Routes({
            "user/login": [reply(400), reply(400), reply(200, fixture("user_login.json"))],
            "track/search": [reply(200, fixture("search.json"))],
            "track/getFileUrl": [reply(200, file_url_payload()), reply(200, file_url_payload()),
                                 reply(400, {"message": f"Bad parameter user_auth_token={TOKEN} {S_LONDON}"})],
        })
        cdn = FakeCdn(lambda r: httpx.Response(200, content=b"fLaC"))
        client = make_client(tmp_path, api, cdn=cdn, cache=None)
        user = client.login()
        client.search_tracks("Miles Davis So What")
        stream = client.get_file_url("9", 27)
        client.download("9", tmp_path / "source.flac")
        with pytest.raises(qc.QobuzError) as failed:
            client.get_file_url("9", 27)
        try:
            raise RuntimeError(f"wrapped {TOKEN} {CDN_URL}")
        except RuntimeError:
            logging.getLogger("leasure.test").exception("failure with %s", TOKEN)
    finally:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    signatures = [r.url.params["request_sig"] for r in api.to("getFileUrl")]
    assert "user/login?***" in caplog.text
    forbidden = [TOKEN, S_LONDON, S_BERLIN, APP_SECRET, CDN_QUERY, *signatures]
    texts = [caplog.text, str(failed.value), repr(client), repr(stream), repr(client.app_credentials()), repr(user)]
    for text in texts:
        for secret in forbidden:
            assert secret not in text


def test_redact_patterns_cover_query_header_and_cdn_forms():
    qc.register_secret("short")
    text = qc.redact(f"a?request_sig=abc123&user_auth_token=xyz X-User-Auth-Token: 'zzz' {CDN_URL} short")
    assert "abc123" not in text and "xyz" not in text and "zzz" not in text and CDN_QUERY not in text
    assert "short" in text


def test_redaction_keeps_the_args_uvicorn_access_lines_unpack():
    from uvicorn.logging import AccessFormatter

    qc.install_log_redaction()
    qc.register_secret(TOKEN)
    formatter = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    make = logging.getLogRecordFactory()

    def access_line(path: str) -> str:
        record = make("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                      ("127.0.0.1:5000", "GET", path, "1.1", 200), None)
        return formatter.format(record)

    assert access_line("/api/status") == '127.0.0.1:5000 - "GET /api/status HTTP/1.1" 200 OK'
    assert access_line(f"/x?t={TOKEN}") == '127.0.0.1:5000 - "GET /x?t=*** HTTP/1.1" 200 OK'


def test_login_app_id_400_rescrapes_and_signs_in_with_the_new_app(tmp_path):
    rejected = reply(400, {"message": "Invalid or missing app_id parameter"})
    api = Routes({"user/login": [rejected, rejected, rejected, reply(200, fixture("user_login.json"))]})
    web = FakeWeb(bundle=make_bundle(app_id="987654321"))

    make_client(tmp_path, api, web=web).login()

    assert web.login_hits == 1
    assert api.requests[-1].headers["x-app-id"] == "987654321"


def test_login_app_id_400_with_an_unchanged_app_is_app_error(tmp_path):
    api = Routes({"user/login": [reply(400, {"message": "Invalid or missing app_id parameter"})]})
    web = FakeWeb()

    with pytest.raises(qc.QobuzAppError):
        make_client(tmp_path, api, web=web).login()

    assert web.login_hits == 1 and len(api.requests) == 3


def test_a_long_400_message_never_keeps_part_of_a_token(tmp_path):
    message = "Invalid parameter value " + "x" * 160 + " " + TOKEN
    api = Routes({"track/search": [reply(400, {"message": message})]})

    with pytest.raises(qc.QobuzError) as exc:
        make_client(tmp_path, api).search_tracks("x")

    assert not any(TOKEN[i:i + 12] in str(exc.value) for i in range(len(TOKEN) - 11))


@pytest.mark.parametrize("status", [403, 404, 410])
def test_a_cdn_that_keeps_refusing_fails_after_one_new_url(tmp_path, status):
    api = file_url_routes()
    cdn = FakeCdn(lambda r: httpx.Response(status))
    dest = tmp_path / "source.flac"

    with pytest.raises(qc.QobuzDownloadError, match="refused the download"):
        make_client(tmp_path, api, cdn=cdn).download("9", dest)

    assert (len(api.to("getFileUrl")), len(cdn.requests)) == (2, 2)
    assert list(tmp_path.glob("source.flac*")) == []
