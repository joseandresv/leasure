import json
from dataclasses import dataclass, field

import pytest

from config import settings
from scripts import qobuz_check
from services import qobuz_client, qobuz_session
from services.qobuz_client import AppCredentials, FileUrl, QobuzUser
from tests.qobuz_fakes import TOKEN, qtrack, store_session

SECRET = "0123456789abcdef0123456789abcdef"
LOGIN_RAW = {"user": {"id": 12344321, "display_name": "pepe", "email": "pepe@example.com", "country_code": "ES",
                      "credential": {"label": "Studio", "description": "Qobuz Studio",
                                     "parameters": {"lossless_streaming": True, "hires_streaming": True,
                                                    "short_label": "Studio"}},
                      "subscription": {"offer": "studio", "end_date": "2027-01-01"}}}


@dataclass
class ScriptClient:
    calls: list = field(default_factory=list)

    def login(self):
        return QobuzUser(user_id="12344321", display_name="pepe", country="ES", plan="Studio", lossless=True,
                         hires=True, parameters={}, raw=LOGIN_RAW)

    def search_tracks(self, query, limit=10):
        return [qtrack(raw={"id": 1, "title": "So What", "note": f"signed with {SECRET}"})]

    def resolve_stream(self, track_id, max_format_id=27, **_):
        return FileUrl(url=f"https://streaming-qobuz-std.akamaized.net/file?sig={SECRET}", format_id=27,
                       requested_format_id=27, mime_type="audio/flac", bit_depth=24, sampling_rate=192.0,
                       duration_s=562, raw={"track_id": 1, "format_id": 27, "echo": [f"x{TOKEN}y"]})

    def app_credentials(self, **_):
        return AppCredentials(app_id="123456789", secrets=(SECRET,), secret=SECRET, login_shape="query")

    def close(self):
        pass


@pytest.fixture
def script_client(monkeypatch):
    store_session()
    fake = ScriptClient()
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: fake)
    monkeypatch.setattr(qobuz_check.httpx, "get", lambda *a, **k: pytest.fail("no network in tests"))
    return fake


def test_redact_login_keeps_only_the_plan_fields():
    redacted = qobuz_check.redact_login(LOGIN_RAW)

    assert redacted["user"]["id"] == qobuz_check.FAKE_USER_ID and redacted["user"]["display_name"] == "fixture-user"
    assert "email" not in json.dumps(redacted) and "end_date" not in json.dumps(redacted)
    assert redacted["user"]["credential"]["parameters"] == {"lossless_streaming": True, "hires_streaming": True}


def test_redact_replaces_secrets_inside_longer_strings_lists_and_urls():
    value = {"a": [f"pre{SECRET}post", {"deep": f"https://x.akamaized.net/f?s={SECRET}"}],
             "url": "https://example.com/plain", "user_auth_token": "anything"}

    redacted = json.dumps(qobuz_check.redact(value, [SECRET]))

    assert SECRET not in redacted and redacted.count("***") >= 4 and "example.com" not in redacted


def test_the_report_hides_the_user_and_every_secret(script_client, capsys):
    assert qobuz_check.main([]) == 0

    out = capsys.readouterr().out
    assert "12344321" not in out and "pepe" not in out and "…4321" in out
    assert SECRET not in out and TOKEN not in out
    assert '"url_host": "streaming-qobuz-std.akamaized.net"' in out


def test_saved_fixtures_go_to_data_qobuz_fixtures_redacted(script_client):
    qobuz_check.main(["--save-fixtures"])

    folder = settings.data_dir / "qobuz-fixtures"
    written = {p.name: p.read_text() for p in folder.iterdir()}
    assert set(written) == {"user_login.json", "search.json", "get_file_url_27.json"}
    assert not any(SECRET in text or TOKEN in text or "12344321" in text for text in written.values())


def test_without_a_sign_in_it_explains_and_exits_2(capsys, monkeypatch):
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: pytest.fail("no client expected"))

    assert qobuz_check.main([]) == 2
    assert "Qobuz card" in capsys.readouterr().out


def test_a_preview_only_stream_fails_the_check(script_client, monkeypatch):
    def preview(*_args, **_kwargs):
        raise qobuz_client.QobuzNotStreamableError(reason="sample")

    monkeypatch.setattr(script_client, "resolve_stream", preview)

    assert qobuz_check.main([]) == 1
