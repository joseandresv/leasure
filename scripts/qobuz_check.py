"""Show what Qobuz really answers for the stored sign-in: app credentials, login shape and plan,
a search, and the stream a track resolves to. Everything printed or saved is redacted.

    python -m scripts.qobuz_check [--isrc ISRC | --query "artist title"] [--format 27] [--probe-cdn]
                                  [--save-fixtures [DIR]]
    python -m scripts.qobuz_check --favorites [--save-fixtures [DIR]]

Exits 0 when the login works, the probe gets a full lossless FLAC stream and a secret signs;
1 otherwise; 2 when Leasure has no Qobuz sign-in.

--favorites makes no search, stream or CDN call: it reads the favorites and playlists endpoints
(about 12 calls), prints counts, booleans, HTTP statuses and field names only, and writes the per-kind
verdict to data/qobuz_favorites_check.json. It exits 0 when the login works and favorite tracks,
favorite albums and the playlist list all answer in the expected shape; 1 otherwise; 2 without a sign-in.
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from config import settings  # noqa: E402
from services import qobuz_client, qobuz_session  # noqa: E402

FAKE_USER_ID = 1000001
FAKE_NAME = "fixture-user"
SECRET_KEYS = frozenset({"url", "request_sig", "user_auth_token", "userauthtoken", "token", "secret", "secrets",
                         "app_secret", "appsecret"})
DEFAULT_FIXTURE_DIR = settings.data_dir / "qobuz-fixtures"
FAVORITES_CHECK_PATH = settings.data_dir / "qobuz_favorites_check.json"
TRACK_FIELDS = ("album", "album.image", "isrc", "version", "streamable", "maximum_bit_depth")
REQUIRED_KINDS = ("tracks_ok", "albums_ok", "playlists_ok")
# Account-level answers: carrying on would only repeat them. A 403 on one playlist or album is not one.
ITEM_STOPPING_ERRORS = (qobuz_client.QobuzAuthError, qobuz_client.QobuzRateLimitError, qobuz_session.SessionChanged)
STOPPING_ERRORS = (*ITEM_STOPPING_ERRORS, qobuz_client.QobuzForbiddenError)
ENUM_KEYS = frozenset({"type", "release_type", "product_type"})
# Album, artist and label slugs are built from names; only a genre's slug is an enum value.
ENUM_PATHS = frozenset({("genre", "slug")})
_ENUM_VALUE = re.compile(r"^[a-z][a-z_-]{0,31}$")
# Scalars under these keys are ids (getUserFavoriteIds answers bare id lists).
ID_LIST_KEYS = frozenset({"tracks", "albums", "artists", "articles", "labels", "playlists"})
FAKE_ID_BASE = 900000
NO_SESSION = ("Leasure has no Qobuz sign-in. Open Leasure, go to Home, and paste your user id and token into "
              "the Qobuz card (play.qobuz.com -> F12 -> Application -> Local Storage -> localuser).")


def redact(value, secrets: list[str]):
    """Every known token and secret replaced wherever it appears, and every secret-bearing key blanked."""
    if isinstance(value, dict):
        return {k: "***" if str(k).casefold() in SECRET_KEYS else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "***")
        return qobuz_client.redact(value)
    return value


def redact_login(raw: dict, *, user_id_hint: str | None = None) -> dict:
    """Only the plan fields of a user/login answer; the id and name become fixed fakes (or a hint)."""
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    credential = user.get("credential") if isinstance(user.get("credential"), dict) else {}
    parameters = credential.get("parameters") if isinstance(credential.get("parameters"), dict) else {}
    subscription = user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    return {"user": {
        "id": user_id_hint or FAKE_USER_ID,
        "display_name": "<redacted>" if user_id_hint else FAKE_NAME,
        "country_code": user.get("country_code"),
        "credential": {"label": credential.get("label"), "description": credential.get("description"),
                       "parameters": {k: v for k, v in parameters.items() if isinstance(v, bool)}},
        "subscription": {"offer": subscription.get("offer")},
    }}


def _track_view(track) -> dict:
    return {"id": track.id, "title": track.full_title, "performer": track.performer, "album": track.album_title,
            "isrc": track.isrc, "duration_s": track.duration_s, "max_bit_depth": track.max_bit_depth,
            "max_sampling_rate": track.max_sampling_rate, "streamable": track.streamable,
            "hires_streamable": track.hires_streamable}


def _probe_cdn(url: str) -> dict:
    """First 64 KiB of the stream, nothing saved. Only https Qobuz CDN hosts, no credentials sent."""
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host.endswith(qobuz_client.CDN_HOST_SUFFIXES):
        return {"error": "unexpected host"}
    try:
        resp = httpx.get(url, headers={"Range": "bytes=0-65535"}, follow_redirects=False, timeout=30)
    except httpx.HTTPError as e:
        return {"error": e.__class__.__name__}
    return {"status": resp.status_code, "header_count": len(resp.headers),
            "content_type": resp.headers.get("content-type"), "bytes_read": len(resp.content)}


def _probe(client, args, report: dict, fixtures: dict) -> bool:
    """Search, then resolve the first streamable hit; True when it is a full lossless FLAC stream."""
    tracks = (client.find_by_isrc(args.isrc) if args.isrc
              else client.search_tracks(args.query or settings.qobuz_check_query, limit=10))
    report["search"] = [_track_view(t) for t in tracks[:5]]
    fixtures["search.json"] = {"tracks": {"items": [t.raw for t in tracks[:5]]}}
    hit = next((t for t in tracks if t.streamable is not False), None)
    if hit is None:
        report["errors"].append("search: no streamable track found")
        return False
    stream = client.resolve_stream(hit.id, args.format)
    report["file_url"] = {"track_id": hit.id, "format_id": stream.format_id, "mime_type": stream.mime_type,
                          "bit_depth": stream.bit_depth, "sampling_rate": stream.sampling_rate,
                          "duration": stream.duration_s, "sample": stream.sample,
                          "restrictions": list(stream.restrictions), "url_host": httpx.URL(stream.url).host}
    fixtures[f"get_file_url_{args.format}.json"] = stream.raw
    if args.probe_cdn:
        report["cdn"] = _probe_cdn(stream.url)
    return (stream.format_id in qobuz_client.LOSSLESS_FORMATS and not stream.sample
            and "flac" in (stream.mime_type or "").casefold())


def _app_view(client, secrets: list[str]) -> dict | None:
    app = client.app_credentials()
    secrets.extend(app.secrets)
    index = app.secrets.index(app.secret) if app.secret in app.secrets else None
    return {"app_id": app.app_id, "bundle_version": app.bundle_version, "secrets_found": len(app.secrets),
            "secret_index": index, "login_shape": app.login_shape}


def _save_fixtures(folder: Path, fixtures: dict, secrets: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in fixtures.items():
        (folder / name).write_text(json.dumps(redact(data, secrets), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Fixtures written to {folder}. Review these files before copying any into tests/fixtures/qobuz/.")


def _is_id_key(key: str) -> bool:
    return key == "id" or key.endswith(("_id", "_ids")) or key in ID_LIST_KEYS


def _is_enum(parent: str, key: str, value: str) -> bool:
    return (key in ENUM_KEYS or (parent, key) in ENUM_PATHS) and bool(_ENUM_VALUE.match(value))


def anonymise(value, key: str = "", ids: dict | None = None, parent: str = ""):
    """Every string becomes "<str>" except short enum values under ENUM_KEYS / ENUM_PATHS, every id a
    fixed fake (the same fake for the same id), numbers and booleans stay."""
    ids = {} if ids is None else ids
    if isinstance(value, dict):
        return {k: anonymise(v, str(k), ids, key) for k, v in value.items()}
    if isinstance(value, list):
        return [anonymise(v, key, ids, parent) for v in value]
    if _is_id_key(key) and isinstance(value, int | str) and not isinstance(value, bool):
        fake = ids.setdefault(str(value), FAKE_ID_BASE + len(ids) + 1)
        return fake if isinstance(value, int) else str(fake)
    if isinstance(value, str):
        return value if _is_enum(parent, key, value) else "<str>"
    return value


def _has(raw, dotted: str) -> bool:
    """The field is there and not null."""
    for part in dotted.split("."):
        if not isinstance(raw, dict) or raw.get(part) is None:
            return False
        raw = raw[part]
    return True


def _has_key(raw, dotted: str) -> bool:
    """The key is there, even when its value is null."""
    *parents, last = dotted.split(".")
    for part in parents:
        raw = raw.get(part) if isinstance(raw, dict) else None
    return isinstance(raw, dict) and last in raw


def _error_text(name: str, e: Exception) -> str:
    """HTTP status and error class only: Qobuz's own messages can name items."""
    status = getattr(e, "status", None)
    return f"{name}: HTTP {status} ({e.__class__.__name__})" if status else f"{name}: {e.__class__.__name__}"


def _step(report: dict, name: str, fn, stopping: tuple = STOPPING_ERRORS):
    try:
        return fn()
    except stopping:
        raise
    except qobuz_client.QobuzError as e:
        report["errors"].append(_error_text(name, e))
        return None


def _signing_view(client, report: dict) -> dict:
    statuses = dict(client.library_attempts)
    secret_available = bool(client.app_credentials().secret)
    untested = statuses.get("unsigned") not in (None, 200) and "signed" not in statuses
    if untested and not secret_available:
        report["errors"].append("signing not tried: no proven secret in the app cache "
                                "(run python -m scripts.qobuz_check once to prove one)")
    return {"unsigned_status": statuses.get("unsigned"), "signed_status": statuses.get("signed"),
            "needs_signing": None if untested else statuses.get("signed") == 200, "secret_available": secret_available}


def _cap(page, wanted: int | None) -> int | None:
    """The server's page cap when it sent fewer items than asked and than exist; None when none was seen."""
    if page is None or wanted is None:
        return None
    return page.raw_count if page.raw_count < wanted else None


def _favorite_pages(client, report: dict, fixtures: dict) -> dict:
    pages: dict = {}
    for kind in qobuz_client.FAVORITE_TYPES:
        pages[kind] = _step(report, f"favorites {kind}", lambda kind=kind: client.favorites(kind, limit=5))
        if kind == "tracks":
            report["signing"] = _signing_view(client, report)
        page = pages[kind]
        report["favorites"][kind] = None if page is None else {
            "total": page.total, "items_parsed": len(page.items),
            "item_keys": sorted(page.items[0].raw) if page.items else []}
        if page is not None:
            fixtures[f"favorites_{kind}.json"] = page.raw
    return pages


def _track_facts(report: dict, pages: dict) -> None:
    items = [i for page in pages.values() if page is not None for i in page.items]
    report["has_favorited_at"] = any(i.favorited_at is not None for i in items)
    tracks = pages["tracks"].items if pages.get("tracks") else []
    stamps = [t.favorited_at for t in tracks]
    report["newest_first"] = stamps == sorted(stamps, reverse=True) if len(stamps) > 1 and None not in stamps else None
    report["track_fields"] = {f: all(_has_key(t.raw, f) for t in tracks) if tracks else None for f in TRACK_FIELDS}
    report["track_fields_with_value"] = {f: sum(_has(t.raw, f) for t in tracks) for f in TRACK_FIELDS}


def _page_size_probe(client, report: dict, total: int | None) -> int:
    """Asks for 500 tracks once (when there are more than 5); the page size the sync should use."""
    if not total or total <= 5:
        report["page_cap"] = None
        return qobuz_client.LIBRARY_PAGE_SIZE
    try:
        page = client.favorites("tracks", limit=qobuz_client.LIBRARY_PAGE_SIZE)
    except STOPPING_ERRORS:
        raise
    except qobuz_client.QobuzError as e:
        report["page_cap"] = None
        if type(e) is qobuz_client.QobuzError and e.status == 400:
            report["limit_500_refused"] = True
            return qobuz_client.LIBRARY_FALLBACK_PAGE_SIZE
        report["errors"].append(_error_text("favorites tracks limit=500", e))
        return qobuz_client.LIBRARY_PAGE_SIZE
    report["page_cap"] = _cap(page, min(total, qobuz_client.LIBRARY_PAGE_SIZE))
    return qobuz_client.LIBRARY_PAGE_SIZE


def _ids_view(client, report: dict, fixtures: dict, pages: dict) -> bool:
    ids = _step(report, "favorite ids", client.favorite_ids)
    if ids is None:
        report["ids_complete"] = None
        return False
    fixtures["favorite_ids.json"] = ids
    totals = {k: p.total for k, p in pages.items() if p is not None}
    complete = {k: k in ids and totals.get(k) is not None and len(ids[k]) == totals[k] for k in qobuz_client.FAVORITE_TYPES}
    report["ids_complete"] = complete
    report["ids_counts"] = {k: len(v) for k, v in ids.items()}
    return all(complete.values())


def _playlists_view(client, report: dict, fixtures: dict) -> tuple[bool, bool]:
    view = report["playlists"]
    page = _step(report, "playlists", lambda: client.user_playlists(limit=5))
    if page is None:
        return False, False
    fixtures["user_playlists.json"] = page.raw
    view.update(total=page.total, items_parsed=len(page.items),
                has_updated_at=bool(page.items) and all(p.updated_at is not None for p in page.items))
    for name in ("owner", "subscriber"):
        view[f"filter_{name}_ok"] = _step(
            report, f"playlists filter={name}", lambda name=name: client.user_playlists(limit=5, owner_filter=name)) is not None
    if not page.items:
        return True, False
    got = _step(report, "playlist/get", lambda: client.playlist(page.items[0].id, limit=qobuz_client.LIBRARY_PAGE_SIZE),
                ITEM_STOPPING_ERRORS)
    if got is None:
        return True, False
    fixtures["playlist_get.json"] = got[1].raw
    total = got[1].total
    view.update(tracks_total=total, playlist_get_page_cap=_cap(got[1], min(total or 0, qobuz_client.LIBRARY_PAGE_SIZE)))
    return True, True


def _album_view(client, report: dict, fixtures: dict, albums) -> None:
    if albums is None or not albums.items:
        report["album_get"] = None
        return
    got = _step(report, "album/get", lambda: client.album_page(albums.items[0].id), ITEM_STOPPING_ERRORS)
    if got is None:
        report["album_get"] = None
        return
    page = got[1]
    fixtures["album_get.json"] = page.raw
    raw_items = page.raw["tracks"]["items"]
    report["album_get"] = {"tracks_total": page.total, "tracks_complete": page.total == page.raw_count,
                           "track_items_have_album": bool(raw_items) and all(_has(i, "album") for i in raw_items)}


def _favorites_checks(client, report: dict, fixtures: dict) -> dict:
    pages = _favorite_pages(client, report, fixtures)
    _track_facts(report, pages)
    page_size = _page_size_probe(client, report, pages["tracks"].total if pages["tracks"] else None)
    ids_trusted = _ids_view(client, report, fixtures, pages)
    playlists_ok, playlist_get_ok = _playlists_view(client, report, fixtures)
    _album_view(client, report, fixtures, pages["albums"])
    return {"checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **{f"{kind}_ok": pages[kind] is not None for kind in qobuz_client.FAVORITE_TYPES},
            "playlists_ok": playlists_ok, "playlist_get_ok": playlist_get_ok, "ids_trusted": ids_trusted,
            "signed": bool(report["signing"]["needs_signing"]), "page_size": page_size}


def _write_verdict(verdict: dict) -> None:
    FAVORITES_CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FAVORITES_CHECK_PATH.with_name(FAVORITES_CHECK_PATH.name + ".tmp")
    tmp.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    tmp.replace(FAVORITES_CHECK_PATH)


def favorites_main(session, args, secrets: list[str]) -> int:
    report: dict = {"user": f"…{session.user_id[-4:]}", "logged_in": False, "signing": {}, "favorites": {},
                    "playlists": {}, "errors": []}
    fixtures: dict = {}
    verdict = None
    client = qobuz_session.make_client(session)
    try:
        user = client.login()
        secrets.append(user.issued_token or "")
        report["logged_in"] = True
        verdict = _favorites_checks(client, report, fixtures)
    except STOPPING_ERRORS as e:
        report["errors"].append(f"stopped: {e.__class__.__name__}: {e}")
    except qobuz_client.QobuzError as e:
        report["errors"].append(_error_text("login", e))
    finally:
        client.close()

    report["verdict"] = verdict
    print(json.dumps(redact(report, secrets), indent=2, ensure_ascii=False))
    if verdict is not None:
        _write_verdict(verdict)
        print(f"Verdict written to {FAVORITES_CHECK_PATH}.")
    if args.save_fixtures is not None and fixtures:
        ids: dict = {}
        _save_fixtures(args.save_fixtures, {k: anonymise(v, ids=ids) for k, v in fixtures.items()}, secrets)
    failed = [k for k in REQUIRED_KINDS if not (verdict or {}).get(k)]
    if not report["logged_in"] or failed:
        print("FAIL: " + ", ".join(failed if report["logged_in"] else ["login"]))
        return 1
    print("PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    qobuz_client.install_log_redaction()
    parser = argparse.ArgumentParser(description="Check what Qobuz answers for Leasure's stored sign-in.")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--isrc", help="probe the track with this ISRC")
    which.add_argument("--query", help='probe the first hit of this search ("artist title")')
    which.add_argument("--favorites", action="store_true",
                       help="check the favorites and playlists endpoints instead (no search, stream or CDN call)")
    parser.add_argument("--format", type=int, default=settings.qobuz_max_format, choices=(6, 7, 27))
    parser.add_argument("--probe-cdn", action="store_true", help="read the first 64 KiB of the stream (nothing saved)")
    parser.add_argument("--save-fixtures", nargs="?", const=DEFAULT_FIXTURE_DIR, type=Path, metavar="DIR",
                        help=f"write redacted answers (default dir: {DEFAULT_FIXTURE_DIR})")
    args = parser.parse_args(argv)
    if args.favorites and args.probe_cdn:
        parser.error("--probe-cdn does not apply to --favorites")

    session = qobuz_session.load()
    if session is None:
        print(NO_SESSION)
        return 2
    secrets = [session.user_auth_token]
    if args.favorites:
        return favorites_main(session, args, secrets)
    report: dict = {"app": None, "login": None, "search": [], "file_url": None, "errors": []}
    fixtures: dict = {}
    logged_in = lossless = False
    client = qobuz_session.make_client(session)
    try:
        try:
            user = client.login()
            logged_in = True
            secrets.append(user.issued_token or "")
            report["login"] = redact_login(user.raw, user_id_hint=f"…{session.user_id[-4:]}")
            fixtures["user_login.json"] = redact_login(user.raw)
        except qobuz_client.QobuzError as e:
            report["errors"].append(f"login: {e}")
        try:
            lossless = _probe(client, args, report, fixtures)
        except qobuz_client.QobuzNotStreamableError as e:
            report["errors"].append(f"file_url: {e.reason}: {e}")
        except qobuz_client.QobuzError as e:
            report["errors"].append(f"probe: {e}")
        try:
            report["app"] = _app_view(client, secrets)
        except qobuz_client.QobuzError as e:
            report["errors"].append(f"app: {e}")
    finally:
        client.close()

    print(json.dumps(redact(report, secrets), indent=2, ensure_ascii=False))
    if args.save_fixtures is not None:
        _save_fixtures(args.save_fixtures, fixtures, secrets)
    signs = bool(report["app"] and report["app"]["secret_index"] is not None)
    return 0 if logged_in and lossless and signs else 1


if __name__ == "__main__":
    sys.exit(main())
