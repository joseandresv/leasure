import json
import threading
from dataclasses import dataclass

import pytest

from config import settings
from services import qobuz_client, qobuz_session
from services.qobuz_client import FileUrl, QobuzUser
from tests.qobuz_fakes import TOKEN, qtrack, store_session

SESSION_FILE = settings.data_dir / qobuz_session.SESSION_FILE_NAME


def user(lossless=True, hires=True, issued_token=None):
    return QobuzUser(user_id="12344321", display_name="pepe", country="ES", plan="Studio", lossless=lossless,
                     hires=hires, parameters={}, issued_token=issued_token)


@dataclass
class LoginClient:
    login_result: object = None
    stream: object = None
    rotated_token: str | None = None

    def login(self):
        if isinstance(self.login_result, Exception):
            raise self.login_result
        return self.login_result

    def search_tracks(self, query, limit=10):
        return [qtrack()]

    def resolve_stream(self, track_id, max_format_id=27, **_):
        if isinstance(self.stream, Exception):
            raise self.stream
        return self.stream

    def app_credentials(self, **_):
        return qobuz_client.AppCredentials(app_id="123456789")

    def close(self):
        pass


def flac_stream(bits=24, sample=False, format_id=27):
    return FileUrl(url="https://x.akamaized.net/f", format_id=format_id, requested_format_id=27,
                   mime_type="audio/flac", bit_depth=bits, sampling_rate=96.0, duration_s=200, sample=sample)


@pytest.fixture
def login_client(monkeypatch):
    fake = LoginClient(login_result=user(), stream=flac_stream())
    monkeypatch.setattr(qobuz_session, "make_client", lambda session=None: fake)
    return fake


@pytest.mark.parametrize("fields", [
    {"user_id": "12a", "user_auth_token": TOKEN},
    {"user_id": "123", "user_auth_token": "short"},
    {"user_id": "123", "user_auth_token": TOKEN + " <script>"},
    {"localuser": "{not json " + TOKEN},
    {"localuser": json.dumps({"id": 123, "userAuthToken": "x y"})},
])
def test_malformed_credentials_are_refused_without_echoing_them(fields):
    with pytest.raises(qobuz_session.SessionInputError) as refused:
        qobuz_session.parse_credentials(**fields)

    assert TOKEN not in str(refused.value) and "x y" not in str(refused.value)


def test_localuser_json_gives_the_id_and_token():
    localuser = json.dumps({"id": 12344321, "userAuthToken": TOKEN, "email": "x"})

    assert qobuz_session.parse_credentials(localuser=localuser) == ("12344321", TOKEN)


def test_an_oversized_localuser_is_refused():
    with pytest.raises(qobuz_session.SessionInputError, match="too long"):
        qobuz_session.parse_credentials(localuser="x" * 9000)


def test_save_writes_through_write_secret_and_records_protection(monkeypatch):
    written = []

    def fake_write(path, text):
        written.append(path)
        path.write_text(text(False))
        return False

    monkeypatch.setattr(qobuz_session.secret_file, "write_secret", fake_write)
    store_session()

    assert written[0] == SESSION_FILE
    assert json.loads(SESSION_FILE.read_text())["file_protected"] is False
    assert qobuz_session.status()["file_protected"] is False


def test_status_never_contains_the_token():
    store_session()

    status = qobuz_session.status()

    assert TOKEN not in json.dumps(status)
    assert status["state"] == "connected" and status["user_id_hint"] == "…4321"


def test_sign_in_stores_a_connected_session_after_a_flac_probe(login_client):
    result = qobuz_session.sign_in("12344321", TOKEN)

    assert result["state"] == "connected" and result["plan"] == "Studio"
    assert qobuz_session.load().user_auth_token == TOKEN
    assert qobuz_session.cached_check()["state"] == "pass"


def test_sign_in_takes_lossless_from_the_probe_when_the_plan_does_not_say(login_client):
    login_client.login_result = user(lossless=None, hires=None)

    result = qobuz_session.sign_in("12344321", TOKEN)

    assert (result["state"], result["lossless"], result["hires"]) == ("connected", True, True)


def test_sign_in_without_lossless_and_a_failing_probe_is_ineligible(login_client):
    login_client.login_result = user(lossless=False, hires=False)
    login_client.stream = qobuz_client.QobuzNotStreamableError(reason="lossy_only")

    assert qobuz_session.sign_in("12344321", TOKEN)["state"] == "ineligible"


def test_a_refused_sign_in_stores_nothing(login_client):
    login_client.login_result = qobuz_client.QobuzAuthError("Qobuz refused the sign-in.")

    with pytest.raises(qobuz_client.QobuzAuthError):
        qobuz_session.sign_in("12344321", TOKEN)

    assert not SESSION_FILE.exists()


def test_a_token_qobuz_issued_at_sign_in_is_the_one_stored(login_client):
    login_client.login_result = user(issued_token="newtok_" + "Z" * 30)

    qobuz_session.sign_in("12344321", TOKEN)

    stored = qobuz_session.load()
    assert stored.user_auth_token == "newtok_" + "Z" * 30 and stored.rotated_at


def test_a_stale_fingerprint_cannot_expire_a_newer_sign_in():
    old = store_session()
    store_session(user_auth_token="other_" + "Q" * 30)

    qobuz_session.mark_expired("expired", qobuz_session.token_fingerprint(old.user_auth_token))

    assert qobuz_session.load().state == "connected"


def test_note_error_after_sign_out_does_not_recreate_the_file():
    session = store_session()
    qobuz_session.clear()

    qobuz_session.note_error("late error", qobuz_session.token_fingerprint(session.user_auth_token))

    assert not SESSION_FILE.exists()


def test_concurrent_error_notes_keep_the_file_valid_json():
    session = store_session()
    fingerprint = qobuz_session.token_fingerprint(session.user_auth_token)
    threads = [threading.Thread(target=qobuz_session.note_error, args=(f"error {i}", fingerprint)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert json.loads(SESSION_FILE.read_text())["last_error"].startswith("error ")


def test_clear_removes_stray_temp_files_and_bumps_the_generation():
    store_session()
    stray = settings.data_dir / f".{qobuz_session.SESSION_FILE_NAME}.abc.tmp"
    stray.write_text("half")
    before = qobuz_session.generation()

    assert qobuz_session.clear() is True

    assert not stray.exists() and not SESSION_FILE.exists()
    assert qobuz_session.generation() == before + 1


def test_the_client_guard_stops_after_sign_out(monkeypatch):
    built = {}

    class Recorder:
        def __init__(self, *args, guard=None, **kwargs):
            built["guard"] = guard

    monkeypatch.setattr(qobuz_client, "QobuzClient", Recorder)
    qobuz_session.make_client(store_session())
    built["guard"]()

    qobuz_session.clear()

    with pytest.raises(qobuz_session.SessionChanged):
        built["guard"]()


def test_run_check_marks_a_refused_token_expired(login_client):
    store_session()
    login_client.login_result = qobuz_client.QobuzAuthError("refused")

    assert qobuz_session.run_check()["state"] == "fail"
    assert qobuz_session.load().state == "expired"
    assert qobuz_session.usable() is None


def test_the_stored_protection_flag_describes_the_write_on_disk(monkeypatch):
    results = iter([True, False, True])
    hardened = []

    def flaky_harden(_path):
        hardened.append(next(results))
        return hardened[-1]

    monkeypatch.setattr(qobuz_session.secret_file, "harden", flaky_harden)
    store_session()

    assert json.loads(SESSION_FILE.read_text())["file_protected"] is hardened[-1]
    assert qobuz_session.status()["file_protected"] is hardened[-1]


def test_run_check_stores_a_token_qobuz_rotated(login_client):
    store_session()
    login_client.rotated_token = "tok_new_" + "X" * 30

    qobuz_session.run_check()

    stored = qobuz_session.load()
    assert stored.user_auth_token == login_client.rotated_token and stored.rotated_at


def test_an_expired_sign_in_asks_for_a_new_token_on_the_card_itself(login_client):
    store_session()
    login_client.login_result = qobuz_client.QobuzAuthError("refused")

    qobuz_session.run_check()

    assert qobuz_session.load().last_error == "Your Qobuz sign-in expired. Paste a new token below."
