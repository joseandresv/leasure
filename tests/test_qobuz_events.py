"""Daily shares as fractions of the cap, and the data/qobuz_events.jsonl experiment log."""
import json

from config import settings
from services import qobuz_engine


def events() -> list[dict]:
    return [json.loads(line) for line in qobuz_engine.EVENTS_FILE.read_text().splitlines()]


def test_shares_scale_with_the_cap(monkeypatch):
    assert (qobuz_engine.daily_share("bulk"), qobuz_engine.daily_share("upgrade")) == (60, 20)
    monkeypatch.setattr(settings, "qobuz_max_downloads_per_day", 10000)
    assert (qobuz_engine.daily_share("bulk"), qobuz_engine.daily_share("upgrade")) == (6000, 2000)
    assert qobuz_engine.daily_share("interactive") is None


def test_a_raised_cap_lets_upgrades_past_twenty(monkeypatch):
    monkeypatch.setattr(settings, "qobuz_max_downloads_per_day", 1000)
    assert all(qobuz_engine.reserve_daily_slot("upgrade") for _ in range(21))
    assert qobuz_engine.share_left("upgrade") == 179


def test_a_stream_appends_a_stream_event():
    qobuz_engine.reserve_daily_slot("interactive")
    qobuz_engine.reserve_daily_slot("upgrade")

    assert [(e["event"], e["kind"], e["count"]) for e in events()] == [("stream", "interactive", 1),
                                                                       ("stream", "upgrade", 2)]


def test_a_limit_event_counts_todays_and_recent_streams():
    for _ in range(3):
        qobuz_engine.reserve_daily_slot("upgrade")

    qobuz_engine.record_limit(429, "track/getFileUrl", retry_after=45.0, reason="rate limited")

    limit = events()[-1]
    assert {k: limit[k] for k in ("event", "status", "where", "retry_after", "count", "streams_last_hour",
                                  "streams_last_24h")} == {"event": "limit", "status": 429, "where": "track/getFileUrl",
                                                           "retry_after": 45.0, "count": 3, "streams_last_hour": 3,
                                                           "streams_last_24h": 3}


def test_an_opened_breaker_is_a_limit_event():
    qobuz_engine._open_breaker("cdn_unavailable")

    assert [(e["status"], e["reason"]) for e in events()] == [("breaker", "cdn_unavailable")]


def test_limit_reasons_are_redacted():
    qobuz_engine.record_limit(403, "cdn", reason="https://x.akamaized.net/f?hmac=abc user_auth_token=secret")

    assert "abc" not in events()[0]["reason"] and "secret" not in events()[0]["reason"]


def test_an_unwritable_event_log_never_stops_a_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(qobuz_engine, "EVENTS_FILE", tmp_path / "missing" / "qobuz_events.jsonl")

    assert qobuz_engine.reserve_daily_slot("bulk") is True
