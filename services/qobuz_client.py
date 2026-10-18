"""Minimal direct Qobuz client (sync httpx; callers run it in asyncio.to_thread).

Written from the publicly documented web-player algorithm: app_id and secrets scraped from
the play.qobuz.com bundle, getFileUrl signed with md5. No config/db imports: the engine and
session modules inject paths, writers, throttle and guard.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

IMPLEMENTED = True
API_BASE = "https://www.qobuz.com/api.json/0.2/"
WEB_PLAYER_LOGIN_URL = "https://play.qobuz.com/login"
WEB_PLAYER_ORIGIN = "https://play.qobuz.com"
FORMAT_MP3_320, FORMAT_FLAC_16_44, FORMAT_FLAC_24_96, FORMAT_FLAC_24_192 = 5, 6, 7, 27
LOSSLESS_FORMATS = frozenset({6, 7, 27})
CDN_HOST_SUFFIXES = (".akamaized.net", ".qobuz.com")
FAT32_LIMIT = 4 * 1024**3 - 16 * 1024**2

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
WEB_BODY_LIMIT = 20 * 1024**2
CHUNK_SIZE = 1024**2
MAX_CDN_REDIRECTS = 5
LOGIN_SHAPES = ("header", "form", "query")
LOWER_FORMAT = {27: 7, 7: 6}

APP_ERROR_MESSAGE = "Qobuz changed its web player and Leasure cannot sign requests right now."
NETWORK_ERROR_MESSAGE = "Could not reach Qobuz — check the network connection."
CDN_ERROR_MESSAGE = "The Qobuz download failed on the network — retry later."

_BUNDLE_URL_RE = re.compile(r'<script src="(/resources/(\d+\.\d+\.\d+-[a-z]\d{3})/bundle\.js)"')
_BUNDLE_PATH_RE = re.compile(r"^/resources/[\w.-]+/bundle\.js$")
_APP_ID_RE = re.compile(r'production:\{api:\{appId:"(\d{9})",appSecret:"(\w{32})"')
_SEED_RE = re.compile(r'[a-z]\.initialSeed\("([\w=]+)",window\.utimezone\.([a-z]+)\)')
_SECRET_RE = re.compile(r"^[0-9a-f]{32}$")
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class QobuzError(Exception):
    """str() is a user-safe sentence: never a token, secret, signature or signed URL."""
    status: int | None = None

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status


class QobuzAuthError(QobuzError): ...
class QobuzAppError(QobuzError): ...
class QobuzForbiddenError(QobuzError): ...


class QobuzRateLimitError(QobuzError):
    retry_after: float | None = None

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class QobuzNotFoundError(QobuzError): ...


NOT_STREAMABLE_MESSAGES = {
    "sample": "Qobuz only offers a 30-second preview of this track.",
    "no_url": "Qobuz gave no stream for this track.",
    "lossy_only": "Qobuz only streams this track in a lossy format.",
    "not_flac": "Qobuz offered this track in a format that is not FLAC.",
    "restricted": "Qobuz does not stream this track to this account or region.",
    "too_high": "Qobuz only offers this track above 192 kHz, which the H2 cannot play.",
    "too_large": "This track is too large for a FAT32 card even at 16-bit.",
    "not_streamable": "Qobuz cannot stream this track.",
}


class QobuzNotStreamableError(QobuzError):
    reason: str = "not_streamable"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        # QobuzNotStreamableError("restricted") reads as a reason code, not a sentence.
        if reason is None and message in NOT_STREAMABLE_MESSAGES:
            reason, message = message, ""
        reason = reason or "not_streamable"
        super().__init__(message or NOT_STREAMABLE_MESSAGES.get(reason, NOT_STREAMABLE_MESSAGES["not_streamable"]))
        self.reason = reason


class QobuzNetworkError(QobuzError): ...
class QobuzDownloadError(QobuzError): ...


class _SignatureRejected(QobuzAppError):
    """getFileUrl refused the request signature: the caller tries the next secret."""


class _RestartStream(Exception):
    """The CDN refused the signed URL before any byte arrived."""


class _StreamBroke(Exception):
    """The CDN connection broke mid-body."""


class _StepDown(Exception):
    """The file would not fit a FAT32 card at this format."""


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    secrets: tuple[str, ...] = field(default=(), repr=False)
    secret: str | None = field(default=None, repr=False)
    bundle_version: str | None = None
    login_shape: str | None = None
    fetched_at: str = ""


@dataclass(frozen=True)
class QobuzUser:
    user_id: str
    display_name: str | None
    country: str | None
    plan: str | None
    lossless: bool | None
    hires: bool | None
    parameters: dict
    issued_token: str | None = field(default=None, repr=False, compare=False)
    raw: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class QobuzTrack:
    id: str
    title: str
    version: str | None
    performer: str
    album_title: str | None
    album_id: str | None
    album_artist: str | None
    album_upc: str | None
    isrc: str | None
    duration_s: int | None
    track_number: int | None
    media_number: int | None
    max_bit_depth: int | None
    max_sampling_rate: float | None
    streamable: bool | None
    hires_streamable: bool | None
    cover_url: str | None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def full_title(self) -> str:
        if self.version and self.version.casefold() not in self.title.casefold():
            return f"{self.title} ({self.version})"
        return self.title


@dataclass(frozen=True)
class FileUrl:
    url: str = field(repr=False)
    format_id: int = 0
    requested_format_id: int = 0
    mime_type: str | None = None
    bit_depth: int | None = None
    sampling_rate: float | None = None
    duration_s: int | None = None
    sample: bool = False
    restrictions: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    size: int
    content_length: int | None
    stream: FileUrl


class Throttle:
    calls_per_minute: float

    def __init__(self, calls_per_minute: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.calls_per_minute = calls_per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at: float | None = None

    def wait(self) -> None:
        # Sleeping while holding the lock is the point: it serialises every worker thread.
        with self._lock:
            now = self._clock()
            if self._next_at is not None and now < self._next_at:
                self._sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + 60.0 / self.calls_per_minute


_shared_throttle: Throttle | None = None
_shared_lock = threading.Lock()
_scrape_lock = threading.Lock()
_last_scrape_at: float | None = None
_secrets: set[str] = set()
_redaction_lock = threading.Lock()
_redaction_installed = False


def shared_throttle(calls_per_minute: float) -> Throttle:
    global _shared_throttle
    with _shared_lock:
        if _shared_throttle is None:
            _shared_throttle = Throttle(calls_per_minute)
        else:
            _shared_throttle.calls_per_minute = calls_per_minute
        return _shared_throttle


def normalise_isrc(value: str | None) -> str | None:
    if not value:
        return None
    isrc = re.sub(r"[\s-]", "", str(value)).upper()
    return isrc if _ISRC_RE.match(isrc) else None


def request_signature(track_id: str, format_id: int, timestamp: str, secret: str) -> str:
    raw = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{timestamp}{secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def parse_bundle_url(login_html: str) -> tuple[str, str]:
    """(bundle path, bundle version) from the web player's login page."""
    m = _BUNDLE_URL_RE.search(login_html)
    if not m or not _BUNDLE_PATH_RE.match(m.group(1)):
        raise QobuzAppError(APP_ERROR_MESSAGE)
    return m.group(1), m.group(2)


def parse_app_credentials(bundle_js: str) -> tuple[str, list[str]]:
    """(app_id, secrets in try-order) from the web player bundle."""
    app = _APP_ID_RE.search(bundle_js)
    if not app:
        raise QobuzAppError(APP_ERROR_MESSAGE)
    register_secret(app.group(2))
    seeds: dict[str, str] = {}
    for seed, tz in _SEED_RE.findall(bundle_js):
        seeds.setdefault(tz, seed)
    order = list(seeds)
    if len(order) > 1:
        order.insert(0, order.pop(1))
    secrets: list[str] = []
    for tz in order:
        zone = re.search(rf'name:"\w+/({re.escape(tz.capitalize())})",info:"([\w=]+)",extras:"([\w=]+)"', bundle_js)
        if not zone:
            continue
        secret = _decode_secret(seeds[tz] + zone.group(2) + zone.group(3))
        if secret and secret not in secrets:
            secrets.append(secret)
            register_secret(secret)
    if not secrets:
        raise QobuzAppError(APP_ERROR_MESSAGE)
    return app.group(1), secrets


def _decode_secret(joined: str) -> str | None:
    try:
        secret = base64.standard_b64decode(joined[:-44]).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    return secret if _SECRET_RE.match(secret) else None


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def register_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        _secrets.add(value)


_REDACTIONS = (
    (re.compile(r"request_sig=[0-9A-Fa-f]+"), "request_sig=***"),
    (re.compile(r"user_auth_token=[^&\s\"']+"), "user_auth_token=***"),
    (re.compile(r"(?i)x-user-auth-token['\"]?\s*[:=]\s*['\"]?[^'\"\s,}]+"), "x-user-auth-token: ***"),
    # Signed CDN URLs: Akamai and any Qobuz-hosted edge (CDN_HOST_SUFFIXES).
    (re.compile(r"(https?://[^\s?\"'/]*(?:akamaized\.net|qobuz\.com)[^\s?\"']*)\?[^\s\"']*"), r"\1?***"),
)


def redact(text: str) -> str:
    if not text:
        return text
    for value in sorted(_secrets, key=len, reverse=True):
        text = text.replace(value, "***")
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _redacted_format(record: logging.LogRecord, redacted: str) -> tuple[object, tuple]:
    """(msg, args) that render `redacted`. The args tuple stays when redacting each value is enough:
    formatters such as uvicorn's access log unpack record.args."""
    if isinstance(record.args, tuple) and record.args:
        msg = redact(str(record.msg))
        args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        try:
            if msg % args == redacted:
                return msg, args
        except (TypeError, ValueError):
            pass
    return redacted, ()


def _redact_record(record: logging.LogRecord) -> None:
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    redacted = redact(message)
    if redacted != message:
        record.msg, record.args = _redacted_format(record, redacted)
    if record.exc_info:
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def install_log_redaction() -> None:
    """Rewrite every log record (message, exception and stack text) through redact()."""
    global _redaction_installed
    with _redaction_lock:
        if _redaction_installed:
            return
        base_factory = logging.getLogRecordFactory()

        def factory(*args, **kwargs) -> logging.LogRecord:
            record = base_factory(*args, **kwargs)
            _redact_record(record)
            return record

        logging.setLogRecordFactory(factory)
        _redaction_installed = True


def _reset_state_for_tests() -> None:
    global _shared_throttle, _last_scrape_at
    with _shared_lock:
        _shared_throttle = None
    _last_scrape_at = None
    _secrets.clear()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _khz(value) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate / 1000 if rate > 1000 else rate


def _bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _without_token(value):
    if isinstance(value, dict):
        return {k: _without_token(v) for k, v in value.items() if k != "user_auth_token"}
    if isinstance(value, list):
        return [_without_token(v) for v in value]
    return value


def _safe_cover_url(url) -> str | None:
    if not isinstance(url, str) or ".." in url or "%2e" in url.lower():
        return None
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return None
    if (u.scheme != "https" or u.host != "static.qobuz.com" or u.port is not None or u.userinfo
            or not u.path.startswith("/images/") or ".." in u.path):
        return None
    return str(u)


def _parse_track(item: dict) -> QobuzTrack | None:
    if not isinstance(item, dict) or item.get("id") in (None, ""):
        return None
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    album_artist = (album.get("artist") or {}).get("name") if isinstance(album.get("artist"), dict) else None
    performer = (item.get("performer") or {}).get("name") if isinstance(item.get("performer"), dict) else None
    artist = (item.get("artist") or {}).get("name") if isinstance(item.get("artist"), dict) else None
    image = album.get("image") if isinstance(album.get("image"), dict) else {}
    cover = image.get("large") or image.get("small") or image.get("thumbnail")
    return QobuzTrack(
        id=str(item["id"]),
        title=_text(item.get("title")) or "",
        version=_text(item.get("version")),
        performer=_text(performer) or _text(artist) or _text(album_artist) or "",
        album_title=_text(album.get("title")),
        album_id=str(album["id"]) if album.get("id") not in (None, "") else None,
        album_artist=_text(album_artist),
        album_upc=_text(album.get("upc")),
        isrc=normalise_isrc(item.get("isrc")),
        duration_s=_int(item.get("duration")),
        track_number=_int(item.get("track_number")),
        media_number=_int(item.get("media_number")),
        max_bit_depth=_int(item.get("maximum_bit_depth")),
        max_sampling_rate=_khz(item.get("maximum_sampling_rate")),
        streamable=_bool(item.get("streamable")),
        hires_streamable=_bool(item.get("hires_streamable")),
        cover_url=_safe_cover_url(cover),
        raw=item,
    )


def _parse_file_url(data: dict, requested_format_id: int) -> FileUrl:
    codes = []
    for r in data.get("restrictions") or []:
        code = r.get("code") if isinstance(r, dict) else r
        if isinstance(code, str):
            codes.append(code)
    url = data.get("url")
    return FileUrl(
        url=url if isinstance(url, str) else "",
        format_id=_int(data.get("format_id")) or 0,
        requested_format_id=requested_format_id,
        mime_type=_text(data.get("mime_type")),
        bit_depth=_int(data.get("bit_depth")),
        sampling_rate=_khz(data.get("sampling_rate")),
        duration_s=_int(data.get("duration")),
        sample="sample" in data and data["sample"] is not False,
        restrictions=tuple(codes),
        raw={k: v for k, v in data.items() if k != "url"},
    )


def _check_stream(stream: FileUrl, expected_duration_s: float | None) -> None:
    if stream.sample:
        raise QobuzNotStreamableError(reason="sample")
    if (expected_duration_s and expected_duration_s > 60
            and stream.duration_s is not None and stream.duration_s <= 35):
        raise QobuzNotStreamableError(reason="sample")
    if not stream.url:
        raise QobuzNotStreamableError(reason="no_url")
    if stream.format_id not in LOSSLESS_FORMATS:
        raise QobuzNotStreamableError(reason="lossy_only")
    # A missing mime is tolerated: the engine decodes every file with ffmpeg anyway.
    if stream.mime_type and "flac" not in stream.mime_type.lower():
        raise QobuzNotStreamableError(reason="not_flac")


def _check_cdn_url(url: str) -> httpx.URL:
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        raise QobuzDownloadError("Qobuz sent the file from an unexpected host.") from None
    host = (u.host or "").lower()
    if u.scheme != "https" or u.port is not None or u.userinfo or not host.endswith(CDN_HOST_SUFFIXES):
        raise QobuzDownloadError("Qobuz sent the file from an unexpected host.")
    return u


def _api_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        message = data.get("message") if isinstance(data, dict) else None
    except ValueError:
        message = None
    return message if isinstance(message, str) else resp.text[:500]


def _sanitise(message: str) -> str:
    message = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", message)).strip()
    # Redact before cutting: a cut through a token leaves a prefix redact() no longer recognises.
    return redact(message)[:200] or "Qobuz refused this request."


def _network_guard(error_cls: type[QobuzError], message: str):
    """Public methods never leak an httpx exception (its text can carry a signed URL)."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (httpx.HTTPError, httpx.InvalidURL):
                raise error_cls(message) from None
        return inner
    return wrap


class QobuzClient:
    user_id: str
    rotated_token: str | None

    def __init__(self, user_id: str, user_auth_token: str, *, app_cache_path: Path,
                 write_text: Callable[[Path, str], object] | None = None,
                 throttle: Throttle | None = None,
                 guard: Callable[[], None] | None = None,
                 transport: httpx.BaseTransport | None = None,
                 web_transport: httpx.BaseTransport | None = None,
                 cdn_transport: httpx.BaseTransport | None = None,
                 api_timeout: float = 20.0, download_deadline_s: float = 1200.0,
                 rescrape_interval_s: float = 1800.0,
                 retry_sleep: Callable[[float], None] = time.sleep) -> None:
        self.user_id = str(user_id)
        self._token = user_auth_token
        register_secret(user_auth_token)
        self.rotated_token = None
        self._app_cache_path = Path(app_cache_path)
        self._write_text = write_text or _write_private
        self._throttle = throttle or shared_throttle(20)
        self._guard = guard
        self._download_deadline_s = download_deadline_s
        self._rescrape_interval_s = rescrape_interval_s
        self._retry_sleep = retry_sleep
        self._app: AppCredentials | None = None
        self._scraped_here = False
        self._auth_confirmed: bool | None = None
        headers = {"User-Agent": USER_AGENT}
        self._api_http = httpx.Client(base_url=API_BASE, headers=headers, follow_redirects=False,
                                      timeout=api_timeout, transport=transport)
        self._web = httpx.Client(headers=headers, follow_redirects=True, timeout=api_timeout,
                                 transport=web_transport or transport)
        self._cdn = httpx.Client(headers=headers, follow_redirects=False,
                                 timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10),
                                 transport=cdn_transport or transport)

    @property
    def token_fingerprint(self) -> str:
        return hashlib.sha256(self._token.encode("utf-8")).hexdigest()[:12]

    def close(self) -> None:
        for client in (self._api_http, self._web, self._cdn):
            client.close()

    def __enter__(self) -> QobuzClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"QobuzClient(user_id=…{self.user_id[-4:]})"

    @_network_guard(QobuzNetworkError, NETWORK_ERROR_MESSAGE)
    def app_credentials(self, *, refresh: bool = False) -> AppCredentials:
        if not refresh:
            if self._app is not None:
                return self._app
            cached = self._read_cache()
            if cached is not None:
                self._app = cached
                return cached
        with _scrape_lock:
            cached = self._read_cache()
            if not refresh:
                if cached is None:
                    cached = self._scrape(None)
                self._app = cached
                return cached
            if cached is not None and self._app is not None and \
                    (cached.app_id, cached.secrets) != (self._app.app_id, self._app.secrets):
                self._app = cached
                return cached
            if _last_scrape_at is not None and time.monotonic() - _last_scrape_at < self._rescrape_interval_s:
                raise QobuzAppError(APP_ERROR_MESSAGE)
            self._app = self._scrape(cached or self._app)
            return self._app

    def _read_cache(self) -> AppCredentials | None:
        try:
            data = json.loads(self._app_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        app_id, secrets = data.get("app_id"), data.get("secrets")
        if not isinstance(app_id, str) or not app_id.isdigit() or not isinstance(secrets, list):
            return None
        secrets = tuple(s for s in secrets if isinstance(s, str) and _SECRET_RE.match(s))
        secret = data.get("secret") if isinstance(data.get("secret"), str) else None
        for s in (*secrets, secret):
            register_secret(s)
        shape = data.get("login_shape")
        return AppCredentials(app_id=app_id, secrets=secrets, secret=secret,
                              bundle_version=data.get("bundle_version") if isinstance(data.get("bundle_version"), str) else None,
                              login_shape=shape if shape in LOGIN_SHAPES else None,
                              fetched_at=str(data.get("fetched_at") or ""))

    def _scrape(self, previous: AppCredentials | None) -> AppCredentials:
        global _last_scrape_at
        _last_scrape_at = time.monotonic()
        self._scraped_here = True
        path, version = parse_bundle_url(self._web_text(WEB_PLAYER_LOGIN_URL))
        app_id, secrets = parse_app_credentials(self._web_text(WEB_PLAYER_ORIGIN + path))
        same_app = previous is not None and previous.app_id == app_id
        app = AppCredentials(
            app_id=app_id, secrets=tuple(secrets),
            secret=previous.secret if same_app and previous.secret in secrets else None,
            bundle_version=version,
            login_shape=previous.login_shape if same_app else None,
            fetched_at=_utc_iso())
        logger.info("Qobuz app credentials scraped (bundle %s, %d secrets)", version, len(secrets))
        self._persist(app)
        return app

    def _web_text(self, url: str) -> str:
        with self._web.stream("GET", url) as resp:
            if resp.status_code != 200:
                logger.warning("Qobuz web player answered HTTP %s", resp.status_code)
                raise QobuzAppError(APP_ERROR_MESSAGE)
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > WEB_BODY_LIMIT:
                    raise QobuzAppError(APP_ERROR_MESSAGE)
        return body.decode("utf-8", errors="replace")

    def _persist(self, app: AppCredentials) -> None:
        payload = {"app_id": app.app_id, "secrets": list(app.secrets), "secret": app.secret,
                   "bundle_version": app.bundle_version, "login_shape": app.login_shape, "fetched_at": app.fetched_at}
        try:
            self._write_text(self._app_cache_path, json.dumps(payload, indent=2))
        except OSError as e:
            logger.warning("Could not write the Qobuz app cache %s (errno %s)", self._app_cache_path, e.errno)

    def _update_app(self, **changes) -> None:
        if self._app is None:
            return
        updated = replace(self._app, **changes)
        if updated != self._app:
            self._app = updated
            self._persist(updated)

    def _send(self, method: str, endpoint: str, *, params: dict | None = None, data: dict | None = None,
              token_header: bool = True) -> httpx.Response:
        if self._guard is not None:
            self._guard()
        headers = {"X-App-Id": self.app_credentials().app_id}
        if token_header:
            headers["X-User-Auth-Token"] = self._token
        for attempt in range(2):
            if attempt:
                self._retry_sleep(2)
                if self._guard is not None:
                    self._guard()
            self._throttle.wait()
            try:
                resp = self._api_http.request(method, endpoint, params=params, data=data, headers=headers)
            except (httpx.HTTPError, httpx.InvalidURL):
                raise QobuzNetworkError(NETWORK_ERROR_MESSAGE) from None
            if resp.status_code < 500:
                return resp
            logger.warning("Qobuz %s answered HTTP %s", endpoint, resp.status_code)
        raise QobuzNetworkError("Qobuz is having trouble right now — retry later.", status=resp.status_code)

    def _api(self, method: str, endpoint: str, *, params: dict | None = None, file_url: bool = False,
             app_retry: bool = True) -> dict:
        app_id = self.app_credentials().app_id
        resp = self._send(method, endpoint, params=params)
        status = resp.status_code
        if status == 200:
            return self._json(resp)
        if status == 400:
            message = _api_message(resp)
            lowered = message.lower()
            if "signature" in lowered or "request_sig" in lowered:
                raise _SignatureRejected(APP_ERROR_MESSAGE, status=400)
            if "app_id" in lowered:
                if app_retry and self.app_credentials(refresh=True).app_id != app_id:
                    return self._api(method, endpoint, params=params, file_url=file_url, app_retry=False)
                raise QobuzAppError(APP_ERROR_MESSAGE, status=400)
            raise QobuzError(_sanitise(message), status=400)
        if status == 401:
            if self._confirm_auth():
                raise QobuzAuthError("Qobuz sign-in expired — sign in again.", status=401)
            if file_url:
                raise QobuzNotStreamableError(reason="restricted")
            raise QobuzError("Qobuz refused this request.", status=401)
        if status == 403:
            if file_url:
                raise QobuzNotStreamableError(reason="restricted")
            if self._confirm_auth():
                raise QobuzAuthError("Qobuz sign-in expired — sign in again.", status=401)
            raise QobuzForbiddenError("Qobuz is refusing this account or app right now.", status=403)
        if status == 404:
            raise QobuzNotFoundError("Qobuz has no such item.", status=404)
        if status == 429:
            raise QobuzRateLimitError("Qobuz asked Leasure to slow down.",
                                      retry_after=parse_retry_after(resp.headers.get("retry-after")))
        raise QobuzError("Qobuz refused this request.", status=status)

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise QobuzError("Qobuz sent an unreadable answer.")
        return data

    def _confirm_auth(self) -> bool:
        """True when a fresh login also answers 401: only then is the session really gone."""
        if self._auth_confirmed is None:
            try:
                self.login()
                self._auth_confirmed = False
            except QobuzAuthError:
                self._auth_confirmed = True
            except (QobuzNetworkError, QobuzRateLimitError):
                raise
            except QobuzError:
                self._auth_confirmed = False
        return self._auth_confirmed

    @_network_guard(QobuzNetworkError, NETWORK_ERROR_MESSAGE)
    def login(self) -> QobuzUser:
        app = self.app_credentials()
        outcome = self._login_attempts(app)
        if isinstance(outcome, QobuzUser):
            return outcome
        statuses, app_rejected = outcome
        # All 401 can also mean the web player moved to a new app_id; a 400 naming app_id says so.
        if (app_rejected or all(s == 401 for s in statuses)) and not self._scraped_here:
            try:
                fresh = self.app_credentials(refresh=True)
            except QobuzAppError:
                fresh = app
            if fresh.app_id != app.app_id:
                outcome = self._login_attempts(fresh)
                if isinstance(outcome, QobuzUser):
                    return outcome
                statuses, app_rejected = outcome
        if all(s == 401 for s in statuses):
            raise QobuzAuthError("Qobuz refused this token — copy it again from play.qobuz.com.", status=401)
        if app_rejected:
            raise QobuzAppError(APP_ERROR_MESSAGE, status=400)
        raise QobuzError("Qobuz did not accept the sign-in request.")

    def _login_attempts(self, app: AppCredentials) -> QobuzUser | tuple[list[int], bool]:
        """The user, or (status of each refused shape, whether a 400 named the app_id)."""
        shapes = list(LOGIN_SHAPES)
        if app.login_shape in shapes:
            shapes.insert(0, shapes.pop(shapes.index(app.login_shape)))
        statuses: list[int] = []
        app_rejected = False
        for shape in shapes:
            resp = self._login_request(shape, app.app_id)
            if resp.status_code == 200:
                user = self._parse_login(self._json(resp))
                logger.info("Qobuz login answered 200 (shape %s)", shape)
                self._update_app(login_shape=shape)
                return user
            if resp.status_code == 429:
                raise QobuzRateLimitError("Qobuz asked Leasure to slow down.",
                                          retry_after=parse_retry_after(resp.headers.get("retry-after")))
            logger.info("Qobuz login shape %s answered HTTP %s", shape, resp.status_code)
            statuses.append(resp.status_code)
            app_rejected = app_rejected or (resp.status_code == 400 and "app_id" in _api_message(resp).lower())
        return statuses, app_rejected

    def _login_request(self, shape: str, app_id: str) -> httpx.Response:
        if shape == "header":
            return self._send("POST", "user/login", data={"extra": "partner"})
        credentials = {"user_id": self.user_id, "user_auth_token": self._token, "app_id": app_id}
        if shape == "form":
            return self._send("POST", "user/login", data=credentials, token_header=False)
        # The only shape known to work today; redact() strips the token from any echo of this URL.
        return self._send("GET", "user/login", params=credentials, token_header=False)

    def _parse_login(self, data: dict) -> QobuzUser:
        issued = data.get("user_auth_token")
        if isinstance(issued, str) and issued:
            register_secret(issued)
            if issued != self._token:
                self._token = issued
                self.rotated_token = issued
        else:
            issued = None
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        credential = user.get("credential") if isinstance(user.get("credential"), dict) else {}
        params = credential.get("parameters") if isinstance(credential.get("parameters"), dict) else {}
        subscription = user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
        plan = next((v for v in (credential.get("label"), credential.get("description"), subscription.get("offer"))
                     if isinstance(v, str) and v.strip()), None)
        return QobuzUser(
            user_id=str(user.get("id") or self.user_id),
            display_name=_text(user.get("display_name")),
            country=_text(user.get("country_code")),
            plan=plan,
            lossless=_bool(params.get("lossless_streaming")),
            hires=_bool(params.get("hires_streaming")),
            parameters={k: v for k, v in params.items() if isinstance(v, bool | str | int)},
            issued_token=issued,
            raw=_without_token(data),
        )

    @_network_guard(QobuzNetworkError, NETWORK_ERROR_MESSAGE)
    def search_tracks(self, query: str, limit: int = 10) -> list[QobuzTrack]:
        data = self._api("GET", "track/search", params={"query": query, "limit": limit})
        tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
        items = tracks.get("items") if isinstance(tracks.get("items"), list) else []
        return [t for t in (_parse_track(item) for item in items) if t is not None]

    def find_by_isrc(self, isrc: str, limit: int = 10) -> list[QobuzTrack]:
        wanted = normalise_isrc(isrc)
        if wanted is None:
            return []
        return [t for t in self.search_tracks(wanted, limit) if t.isrc == wanted]

    @_network_guard(QobuzNetworkError, NETWORK_ERROR_MESSAGE)
    def get_track(self, track_id: str) -> QobuzTrack:
        track = _parse_track(self._api("GET", "track/get", params={"track_id": track_id}))
        if track is None:
            raise QobuzError("Qobuz sent an unreadable answer.")
        return track

    @_network_guard(QobuzNetworkError, NETWORK_ERROR_MESSAGE)
    def get_file_url(self, track_id: str, format_id: int) -> FileUrl:
        if self._guard is not None:
            self._guard()
        app = self.app_credentials()
        candidates = self._secret_candidates(app)
        for refreshed in (False, True):
            for secret in candidates:
                try:
                    data = self._signed_file_url(track_id, format_id, secret)
                except _SignatureRejected:
                    logger.info("Qobuz rejected a request signature (track %s, format %s)", track_id, format_id)
                    continue
                self._update_app(secret=secret)
                return _parse_file_url(data, format_id)
            if refreshed:
                break
            fresh = self._secret_candidates(self.app_credentials(refresh=True))
            if fresh == candidates:
                break
            candidates = fresh
        raise QobuzAppError(APP_ERROR_MESSAGE)

    @staticmethod
    def _secret_candidates(app: AppCredentials) -> list[str]:
        ordered = [app.secret] if app.secret else []
        return ordered + [s for s in app.secrets if s != app.secret]

    def _signed_file_url(self, track_id: str, format_id: int, secret: str) -> dict:
        ts = str(int(time.time()))
        params = {"track_id": track_id, "format_id": format_id, "intent": "stream", "request_ts": ts,
                  "request_sig": request_signature(str(track_id), format_id, ts, secret)}
        return self._api("GET", "track/getFileUrl", params=params, file_url=True)

    def resolve_stream(self, track_id: str, max_format_id: int = 27, *, max_sampling_khz: float = 192.0,
                       expected_duration_s: float | None = None) -> FileUrl:
        stream = self.get_file_url(track_id, max_format_id)
        if stream.sampling_rate and stream.sampling_rate > max_sampling_khz and max_format_id == FORMAT_FLAC_24_192:
            stream = self.get_file_url(track_id, FORMAT_FLAC_24_96)
        if stream.sampling_rate and stream.sampling_rate > max_sampling_khz:
            raise QobuzNotStreamableError(reason="too_high")
        _check_stream(stream, expected_duration_s)
        logger.info("Qobuz stream for track %s: format %s (asked %s)", track_id, stream.format_id, stream.requested_format_id)
        return stream

    def download(self, track_id: str, dest: Path, *, max_format_id: int = 27, max_sampling_khz: float = 192.0,
                 expected_duration_s: float | None = None,
                 before_body: Callable[[FileUrl, int | None], None] | None = None,
                 progress: Callable[[int, int | None], None] | None = None) -> DownloadedFile:
        """Stream the FLAC to `dest` (via `dest.part`); hook exceptions propagate unchanged."""
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        try:
            return self._download(track_id, dest, part, max_format_id, max_sampling_khz,
                                  expected_duration_s, before_body, progress)
        except (httpx.HTTPError, httpx.InvalidURL):
            part.unlink(missing_ok=True)
            raise QobuzDownloadError(CDN_ERROR_MESSAGE) from None
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    def _download(self, track_id, dest, part, fmt, max_sampling_khz, expected_duration_s, before_body, progress):
        deadline = time.monotonic() + self._download_deadline_s
        restarted = broke = False
        while True:
            stream = self.resolve_stream(track_id, fmt, max_sampling_khz=max_sampling_khz,
                                         expected_duration_s=expected_duration_s)
            try:
                size, length = self._fetch(stream, part, deadline, expected_duration_s, before_body, progress)
            except _RestartStream:
                if restarted:
                    raise QobuzDownloadError("Qobuz's file server refused the download — retry later.") from None
                restarted = True
                continue
            except _StreamBroke:
                if broke:
                    raise QobuzDownloadError("The Qobuz download kept breaking off — retry later.") from None
                broke = True
                continue
            except _StepDown:
                if fmt not in LOWER_FORMAT:
                    raise QobuzNotStreamableError(reason="too_large") from None
                fmt = LOWER_FORMAT[fmt]
                continue
            os.replace(part, dest)
            return DownloadedFile(path=dest, size=size, content_length=length, stream=stream)

    def _open_cdn(self, url: str) -> httpx.Response:
        for _ in range(MAX_CDN_REDIRECTS + 1):
            target = _check_cdn_url(url)
            try:
                resp = self._cdn.send(self._cdn.build_request("GET", target), stream=True)
            except (httpx.HTTPError, httpx.InvalidURL):
                raise QobuzDownloadError(CDN_ERROR_MESSAGE) from None
            if not resp.has_redirect_location:
                return resp
            resp.close()
            url = str(target.join(resp.headers["location"]))
        raise QobuzDownloadError("Qobuz redirected the download too many times.")

    def _fetch(self, stream, part, deadline, expected_duration_s, before_body, progress) -> tuple[int, int | None]:
        resp = self._open_cdn(stream.url)
        try:
            if resp.status_code in (403, 404, 410):
                raise _RestartStream
            if resp.status_code != 200:
                raise QobuzDownloadError(f"Qobuz's file server answered HTTP {resp.status_code}.", status=resp.status_code)
            length = _int(resp.headers.get("content-length"))
            ceiling = _size_ceiling(stream.duration_s or expected_duration_s)
            if length is not None and length >= FAT32_LIMIT:
                raise _StepDown
            if length is not None and length > ceiling:
                raise QobuzDownloadError("Qobuz offered an implausibly large file.")
            if before_body is not None:
                before_body(stream, length)
            size = self._write_body(resp, part, ceiling, deadline, length, progress)
        finally:
            resp.close()
        if length is not None and size != length:
            raise QobuzDownloadError("The Qobuz download stopped early — retry.")
        return size, length

    @staticmethod
    def _write_body(resp, part: Path, ceiling: float, deadline: float, length: int | None, progress) -> int:
        size = 0
        with open(part, "wb") as fh:
            try:
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    size += len(chunk)
                    if size > ceiling:
                        raise QobuzDownloadError("Qobuz sent more data than a FLAC of this length can hold.")
                    if time.monotonic() > deadline:
                        raise QobuzDownloadError("The Qobuz download took too long — retry later.")
                    fh.write(chunk)
                    if progress is not None:
                        progress(size, length)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout):
                raise _StreamBroke from None
        return size


def _size_ceiling(duration_s: float | None) -> float:
    if not duration_s:
        return 2 * 1024**3
    return min(FAT32_LIMIT, max(64 * 1024**2, duration_s * 192000 * 3 * 2 * 1.1))
