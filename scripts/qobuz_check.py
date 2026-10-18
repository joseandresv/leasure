"""Show what Qobuz really answers for the stored sign-in: app credentials, login shape and plan,
a search, and the stream a track resolves to. Everything printed or saved is redacted.

    python -m scripts.qobuz_check [--isrc ISRC | --query "artist title"] [--format 27] [--probe-cdn]
                                  [--save-fixtures [DIR]]

Exits 0 when the login works, the probe gets a full lossless FLAC stream and a secret signs;
1 otherwise; 2 when Leasure has no Qobuz sign-in.
"""

import argparse
import json
import sys
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


def main(argv: list[str] | None = None) -> int:
    qobuz_client.install_log_redaction()
    parser = argparse.ArgumentParser(description="Check what Qobuz answers for Leasure's stored sign-in.")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--isrc", help="probe the track with this ISRC")
    which.add_argument("--query", help='probe the first hit of this search ("artist title")')
    parser.add_argument("--format", type=int, default=settings.qobuz_max_format, choices=(6, 7, 27))
    parser.add_argument("--probe-cdn", action="store_true", help="read the first 64 KiB of the stream (nothing saved)")
    parser.add_argument("--save-fixtures", nargs="?", const=DEFAULT_FIXTURE_DIR, type=Path, metavar="DIR",
                        help=f"write redacted answers (default dir: {DEFAULT_FIXTURE_DIR})")
    args = parser.parse_args(argv)

    session = qobuz_session.load()
    if session is None:
        print(NO_SESSION)
        return 2
    secrets = [session.user_auth_token]
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
