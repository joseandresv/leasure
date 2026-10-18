"""The Qobuz sign-in Leasure keeps: user id + user_auth_token pasted from play.qobuz.com
(Google sign-in leaves no password to log in with), stored in data/qobuz_session.json."""

import hashlib
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path

from config import settings
from services import qobuz_client, secret_file

logger = logging.getLogger(__name__)

SESSION_FILE_NAME = "qobuz_session.json"
CHECK_FILE_NAME = "qobuz_check.json"
APP_CACHE_NAME = "qobuz_app.json"

MAX_LOCALUSER_CHARS = 8192
_USER_ID = re.compile(r"^\d{1,12}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{16,1024}$")
EXPIRED_MESSAGE = "Your Qobuz sign-in expired. Paste a new token below."
_QUALITY_LABELS = {27: "FLAC up to 24-bit / 192 kHz", 7: "FLAC up to 24-bit / 96 kHz", 6: "FLAC 16-bit / 44.1 kHz"}

qobuz_client.install_log_redaction()


class SessionInputError(ValueError):
    """The pasted sign-in is malformed; the message names the field, never its value."""


class SessionChanged(Exception):
    """Raised by the client guard after a sign-out or a new sign-in."""


@dataclass
class StoredSession:
    user_id: str
    user_auth_token: str = field(repr=False)
    app_id: str | None = None
    source: str = "paste"
    issued_at: str = ""
    state: str = "connected"
    validated_at: str | None = None
    expired_at: str | None = None
    rotated_at: str | None = None
    plan: str | None = None
    lossless: bool | None = None
    hires: bool | None = None
    display_name: str | None = None
    country: str | None = None
    last_error: str | None = None
    file_protected: bool | None = None


_lock = threading.Lock()
_cache: StoredSession | None = None
_cache_loaded = False
_generation = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _session_path() -> Path:
    return settings.data_dir / SESSION_FILE_NAME


def _check_path() -> Path:
    return settings.data_dir / CHECK_FILE_NAME


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def parse_credentials(user_id: str = "", user_auth_token: str = "", localuser: str = "") -> tuple[str, str]:
    """(user_id, token) from the two fields, or from the whole "localuser" JSON value."""
    if localuser.strip():
        if len(localuser) > MAX_LOCALUSER_CHARS:
            raise SessionInputError("The localuser value is too long.")
        try:
            data = json.loads(localuser)
        except ValueError:
            raise SessionInputError("The localuser value is not the JSON copied from play.qobuz.com.") from None
        if not isinstance(data, dict):
            raise SessionInputError("The localuser value is not the JSON copied from play.qobuz.com.")
        user_id, user_auth_token = str(data.get("id") or ""), str(data.get("userAuthToken") or "")
    user_id, user_auth_token = user_id.strip(), user_auth_token.strip()
    if not _USER_ID.match(user_id):
        raise SessionInputError("The User ID must be the number shown as \"id\" in localuser.")
    if not _TOKEN.match(user_auth_token):
        raise SessionInputError("The token does not look like the \"userAuthToken\" value from localuser.")
    return user_id, user_auth_token


def echo_user_id(user_id: str) -> str:
    """The submitted User ID to put back in the form, only when it cannot be a pasted token."""
    user_id = user_id.strip()
    return user_id if _USER_ID.match(user_id) else ""


def _read_file() -> StoredSession | None:
    try:
        data = json.loads(_session_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("user_id") or not data.get("user_auth_token"):
        return None
    known = {f.name for f in fields(StoredSession)}
    try:
        return StoredSession(**{key: value for key, value in data.items() if key in known})
    except TypeError:
        return None


def _write_file(session: StoredSession) -> None:
    """Under _lock. file_protected records whether the hardening worked on this write."""
    def render(protected: bool) -> str:
        session.file_protected = protected
        return json.dumps(asdict(session), indent=2)

    secret_file.write_secret(_session_path(), render)


def _set_cache(session: StoredSession | None) -> None:
    global _cache, _cache_loaded
    _cache, _cache_loaded = session, True
    if session:
        qobuz_client.register_secret(session.user_auth_token)


def load() -> StoredSession | None:
    with _lock:
        if not _cache_loaded:
            _set_cache(_read_file())
        return replace(_cache) if _cache else None


def save(session: StoredSession) -> None:
    global _generation
    with _lock:
        _write_file(session)
        _set_cache(replace(session))
        _generation += 1


def clear() -> bool:
    """Forget the sign-in: session file, last check and any half-written temp file."""
    global _generation
    with _lock:
        path = _session_path()
        existed = path.exists()
        path.unlink(missing_ok=True)
        _check_path().unlink(missing_ok=True)
        for stray in path.parent.glob(f".{SESSION_FILE_NAME}.*.tmp"):
            stray.unlink(missing_ok=True)
        _set_cache(None)
        _generation += 1
        return existed


def usable() -> StoredSession | None:
    session = load()
    return session if session and session.state == "connected" else None


def generation() -> int:
    return _generation


def _mutate(fingerprint: str, change) -> None:
    """Apply `change` to the stored session, unless it was removed or replaced meanwhile."""
    with _lock:
        session = _read_file()
        if session is None or token_fingerprint(session.user_auth_token) != fingerprint:
            return
        change(session)
        _write_file(session)
        _set_cache(session)


def mark_expired(message: str, fingerprint: str) -> None:
    def change(session: StoredSession) -> None:
        session.state, session.expired_at, session.last_error = "expired", _now(), message

    _mutate(fingerprint, change)


def note_error(message: str, fingerprint: str) -> None:
    def change(session: StoredSession) -> None:
        session.last_error = message

    _mutate(fingerprint, change)


def store_rotated_token(fingerprint: str, new_token: str) -> None:
    qobuz_client.register_secret(new_token)

    def change(session: StoredSession) -> None:
        session.user_auth_token, session.rotated_at = new_token, _now()

    _mutate(fingerprint, change)


def make_client(session: StoredSession | None = None) -> qobuz_client.QobuzClient:
    """A client for `session` (default: the stored one) that stops once the sign-in changes."""
    session = session or load()
    if session is None:
        raise SessionChanged("Qobuz is not signed in.")
    built_at = generation()

    def guard() -> None:
        if generation() != built_at:
            raise SessionChanged("The Qobuz sign-in changed while this ran.")

    client = qobuz_client.QobuzClient(
        session.user_id, session.user_auth_token,
        app_cache_path=settings.data_dir / APP_CACHE_NAME,
        write_text=secret_file.write_secret,
        throttle=qobuz_client.shared_throttle(settings.qobuz_api_calls_per_minute),
        guard=guard,
    )
    client.leasure_generation = built_at
    return client


def _probe(client) -> tuple[dict | None, str | None]:
    """Resolve (not download) the check track's stream: (probe facts, error of an app/network failure)."""
    try:
        hits = client.search_tracks(settings.qobuz_check_query, limit=5)
        hit = next((t for t in hits if t.streamable is not False), None)
        if hit is None:
            return None, "The check track was not found on Qobuz."
        probe = {"track": f"{hit.performer} - {hit.title}", "qobuz_id": hit.id, "format_id": None, "bit_depth": None,
                 "sampling_rate": None, "sample": None, "restrictions": [], "lossless": False}
        try:
            stream = client.resolve_stream(hit.id, settings.qobuz_max_format)
        except qobuz_client.QobuzNotStreamableError as e:
            probe["not_streamable"] = e.reason
            return probe, None
        probe.update(format_id=stream.format_id, bit_depth=stream.bit_depth, sampling_rate=stream.sampling_rate,
                     sample=stream.sample, restrictions=list(stream.restrictions),
                     lossless=stream.format_id in qobuz_client.LOSSLESS_FORMATS and not stream.sample)
        return probe, None
    except qobuz_client.QobuzAuthError:
        raise
    except qobuz_client.QobuzError as e:
        return None, str(e)


def _plan_facts(user, probe: dict | None) -> dict:
    lossless, hires = user.lossless, user.hires
    if probe is not None:
        if lossless is None:
            lossless = probe["lossless"]
        if hires is None:
            hires = bool(probe["lossless"] and (probe["bit_depth"] or 0) >= 24)
    ineligible = user.lossless is False and probe is not None and not probe["lossless"]
    return {"lossless": lossless, "hires": hires, "state": "ineligible" if ineligible else "connected",
            "plan": user.plan, "display_name": user.display_name, "country": user.country}


def _check_result(state: str, *, error: str | None = None, facts: dict | None = None,
                  probe: dict | None = None) -> dict:
    facts = facts or {}
    return {"state": state, "checked_at": _now(), "error": error, "plan": facts.get("plan"),
            "lossless": facts.get("lossless"), "hires": facts.get("hires"), "probe": probe}


def _save_check(result: dict) -> dict:
    secret_file.write_atomic(_check_path(), json.dumps(result, indent=2))
    return result


def _app_id(client) -> str | None:
    try:
        return client.app_credentials().app_id
    except qobuz_client.QobuzError:
        return None


def sign_in(user_id: str, user_auth_token: str, *, source: str = "paste") -> dict:
    """Validate a pasted (or, later, app-window) sign-in and store it. Raises QobuzAuthError /
    QobuzError without storing anything when Qobuz refuses it."""
    client = make_client(StoredSession(user_id=user_id, user_auth_token=user_auth_token, source=source))
    try:
        user = client.login()
        probe, probe_error = _probe(client)
        app_id = _app_id(client)
        token = user.issued_token or client.rotated_token or user_auth_token
    finally:
        client.close()
    facts = _plan_facts(user, probe)
    now = _now()
    save(StoredSession(user_id=user.user_id or user_id, user_auth_token=token, app_id=app_id, source=source,
                       issued_at=now, validated_at=now, rotated_at=now if token != user_auth_token else None,
                       last_error=probe_error, **facts))
    passed = bool(probe and probe["lossless"])
    _save_check(_check_result("pass" if passed else ("error" if probe_error else "fail"),
                              error=probe_error, facts=facts, probe=probe))
    return status()


def run_check() -> dict:
    """Log in and resolve the check track again; updates the stored session and caches the verdict."""
    session = load()
    if session is None:
        return _save_check(_check_result("not_configured"))
    fingerprint = token_fingerprint(session.user_auth_token)
    client = make_client(session)
    try:
        user = client.login()
        probe, probe_error = _probe(client)
        if client.rotated_token:
            store_rotated_token(fingerprint, client.rotated_token)
            fingerprint = token_fingerprint(client.rotated_token)
    except qobuz_client.QobuzAuthError:
        mark_expired(EXPIRED_MESSAGE, fingerprint)
        return _save_check(_check_result("fail", error="Qobuz refused the stored token — sign in again."))
    except (qobuz_client.QobuzError, SessionChanged) as e:
        message = qobuz_client.redact(str(e))
        note_error(message, fingerprint)
        return _save_check(_check_result("error", error=message))
    finally:
        client.close()
    facts = _plan_facts(user, probe)

    def change(stored: StoredSession) -> None:
        stored.state, stored.validated_at, stored.expired_at = facts["state"], _now(), None
        stored.plan, stored.lossless, stored.hires = facts["plan"], facts["lossless"], facts["hires"]
        stored.display_name, stored.country, stored.last_error = facts["display_name"], facts["country"], probe_error

    _mutate(fingerprint, change)
    passed = bool(probe and probe["lossless"])
    return _save_check(_check_result("pass" if passed else ("error" if probe_error else "fail"),
                                     error=probe_error, facts=facts, probe=probe))


def cached_check() -> dict:
    try:
        result = json.loads(_check_path().read_text(encoding="utf-8"))
        if isinstance(result, dict) and result.get("state"):
            return result
    except (OSError, ValueError):
        pass
    state = "unchecked" if load() else "not_configured"
    return {"state": state, "checked_at": None, "error": None, "plan": None, "lossless": None, "hires": None,
            "probe": None}


def status() -> dict:
    """QobuzStatus: what the Qobuz card shows. Never the token."""
    from services import qobuz_engine

    session = load()
    common = {
        "breaker": qobuz_engine.breaker_state(),
        "cooldown_left_s": round(qobuz_engine.cooldown_left()),
        "max_format": settings.qobuz_max_format,
        "max_quality_label": _QUALITY_LABELS[settings.qobuz_max_format],
        "downloads_today": qobuz_engine.downloads_today(),
        "daily_cap": settings.qobuz_max_downloads_per_day,
        "api_calls_per_minute": settings.qobuz_api_calls_per_minute,
    }
    if session is None:
        return {"state": "not_configured", "user_id_hint": None, "display_name": None, "country": None, "plan": None,
                "lossless": None, "hires": None, "source": None, "issued_at": None, "validated_at": None,
                "expired_at": None, "rotated_at": None, "last_error": None, "file_protected": None,
                "app_id": None, **common}
    return {"state": session.state, "user_id_hint": f"…{session.user_id[-4:]}",
            "display_name": session.display_name, "country": session.country, "plan": session.plan,
            "lossless": session.lossless, "hires": session.hires, "source": session.source,
            "issued_at": session.issued_at, "validated_at": session.validated_at, "expired_at": session.expired_at,
            "rotated_at": session.rotated_at, "last_error": session.last_error,
            "file_protected": session.file_protected, "app_id": session.app_id, **common}


def _reset_for_tests() -> None:
    global _cache, _cache_loaded, _generation
    with _lock:
        _cache, _cache_loaded, _generation = None, False, 0
