"""Qobuz card, upgrade slots and their refresh wiring, rendered with a bare Jinja environment."""
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)


def _render(name: str, **context) -> str:
    return env.get_template(name).render(**context)


def _card(qobuz: dict, check: dict | None = None, **context) -> str:
    return _render("partials/qobuz_status.html", qobuz=qobuz, check=check or {}, **context)


def _button(**context) -> str:
    base = {"scope": "album", "connected": True, "session_state": "connected", "eligible_ids": [],
            "eligible_count": 0, "queued_count": 0, "failed_count": 0, "message": None, "error": None}
    return _render("partials/qobuz_upgrade_button.html", **{**base, **context})


def test_upgrade_button_renders_nothing_without_the_unmatched_count():
    assert _button().strip() == ""


def test_album_upgrade_offers_a_retry_for_unmatched_tracks():
    html = _button(unmatched_ids=[7], unmatched_count=1)
    assert "Try again on Qobuz (1 not matched before)" in html
    assert 'hx-post="/api/qobuz/upgrade/tracks"' in html and '"include_unmatched": "true"' in html
    assert '"track_ids": "7"' in html


def test_all_upgrade_retry_for_unmatched_asks_first():
    html = _button(scope="all", unmatched_count=3)
    assert 'hx-post="/api/qobuz/upgrade/all"' in html and '"include_unmatched": "true"' in html
    assert 'hx-confirm="Ask Qobuz again for 3 tracks' in html


def test_upgrade_all_confirm_counts_one_track_in_the_singular():
    html = _button(scope="all", eligible_count=1)
    assert 'hx-confirm="Queue 1 track for a FLAC upgrade?' in html


def test_eligible_tracks_win_over_the_unmatched_retry():
    html = _button(eligible_ids=[4], eligible_count=1, unmatched_count=2)
    assert "Upgrade to FLAC (1)" in html and "Try again" not in html


def test_check_error_is_shown_once():
    html = _card({"state": "connected", "last_error": "Qobuz timed out."},
                 {"state": "error", "error": "Qobuz timed out."})
    assert html.count("Qobuz timed out.") == 1


def test_expired_card_hides_the_check_and_opens_the_form():
    expired_message = "Qobuz sign-in expired — sign in again from the Qobuz card on Home."
    for html in (_card({"state": "expired", "last_error": expired_message}),
                 _card({"state": "connected"}, expired=True)):
        assert "Sign in again" in html and '<details class="qobuz-signin" open>' in html
        assert "Check: not run" not in html and "Check now" not in html
        assert "from the Qobuz card on Home" not in html


def test_failed_sign_in_keeps_the_user_id_but_never_the_token():
    html = _card({"state": "not_configured"}, user_id_value="12345", error="Qobuz refused this token")
    assert re.search(r'<input type="text" id="qobuz-user-id"[^>]*value="12345"', html)
    assert "value=" not in re.search(r'<input type="password"[^>]*>', html).group(0)
    assert 'value=""' in _card({"state": "not_configured"})


def test_library_rows_and_slot_refresh_on_qobuz_events():
    html = (TEMPLATES / "panels" / "library.html").read_text(encoding="utf-8")
    slot = re.search(r'<div id="qobuz-upgrade-all-slot"[^>]*>', html).group(0)
    rows = re.search(r'<div [^>]*id="track-list"[^>]*>', html).group(0)
    assert "qobuz:changed from:body" in slot
    assert "qobuz:queued from:body" in rows and 'hx-include="#library-search"' in rows
    assert 'id="library-search"' in html


def _album(**db) -> str:
    track = {"name": "T", "download_status": "done",
             "db": {"id": 4, "quality": "aac_256", "format": "m4a", "upgrade_possible": True, **db}}
    return _render("partials/music_album_detail.html", album={"name": "B"}, tracks=[track],
                   provider="spotify", album_id="abc", is_playlist=False)


def test_album_detail_refreshes_rows_and_upgrade_slot_on_qobuz_events():
    html = _album()
    slot = re.search(r'<div id="qobuz-upgrade-album-slot"[^>]*>', html).group(0)
    assert "qobuz:changed from:body" in slot and '"track_ids": "4"' in slot
    refresh = re.search(r'<div hidden hx-get="/api/music/album/spotify/abc"[^>]*>', html).group(0)
    assert "qobuz:queued[this.closest('dialog').open] from:body" in refresh
    assert 'hx-select="#album-tracks"' in refresh


def test_album_detail_has_no_upgrade_slot_when_nothing_is_possible():
    assert "qobuz-upgrade-album-slot" not in _album(upgrade_possible=False)
