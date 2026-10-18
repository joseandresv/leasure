"""Qobuz-facing templates rendered with a bare Jinja environment (no app import)."""
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)


def _macro(name: str):
    return getattr(env.get_template("macros/components.html").module, name)


def _badge(**row) -> str:
    return str(_macro("quality_badge")(row))


def _render(name: str, **context) -> str:
    return env.get_template(name).render(**context)


def _title(html: str) -> str:
    return re.search(r'title="([^"]*)"', html).group(1)


def test_qobuz_hires_flac_shows_depth_rate_and_hires():
    html = _badge(quality="flac_24_96", format="flac", is_lossless=True, engine_used="qobuz",
                  source_format_id="7", source_bit_depth=24, source_sample_rate=96000)
    assert ">FLAC 24/96" in html and "Hi-Res" in html
    assert "Qobuz" in _title(html) and "format 7" in _title(html) and "24-bit / 96 kHz" in _title(html)


def test_cd_quality_flac_shows_44_1_without_hires():
    html = _badge(quality="flac_16_44", format="flac", is_lossless=True, engine_used="qobuz",
                  source_bit_depth=16, source_sample_rate=44100)
    assert ">FLAC 16/44.1" in html and "Hi-Res" not in html


def test_flac_tier_without_streaminfo_falls_back_to_the_tier():
    assert ">FLAC 24/96" in _badge(quality="flac_24_96", format="flac", is_lossless=True)


def test_imported_lossless_flac_is_plain_flac():
    assert ">FLAC</span>" in _badge(quality="flac_lossless", format="flac", is_lossless=True)


def test_aac_row_explains_the_qobuz_skip():
    html = _badge(quality="aac_256", format="m4a", is_lossless=False, qobuz_skip_reason="no_match")
    assert "no confident match" in _title(html)


def test_cdn_failures_are_named_in_the_tooltip():
    html = _badge(quality="aac_256", format="m4a", is_lossless=False, qobuz_skip_reason="cdn_unavailable")
    assert "downloads were failing" in _title(html)


def test_not_configured_adds_no_skip_text():
    html = _badge(quality="aac_256", format="m4a", is_lossless=False, qobuz_skip_reason="not_configured")
    assert "Qobuz" not in html


def test_upgrade_states_are_marked_on_the_badge():
    assert "→ FLAC" in _badge(quality="aac_256", format="m4a", upgrade_state="queued")
    assert "kept original" in _badge(quality="aac_256", format="m4a", upgrade_state="reverted")
    failed = _badge(quality="aac_256", format="m4a", upgrade_state="error", upgrade_error="No exact match")
    assert 'title="No exact match"' in failed and "&#9888;" in failed


def test_engine_label_names_each_engine():
    label = _macro("engine_label")
    assert [str(label(e)) for e in ("qobuz", "ytmusic", "youtube-search", "youtube", "card-import", "other")] == [
        "Qobuz", "YouTube Music", "YouTube search", "YouTube", "Card import", "other"]


def test_failed_upgrade_retries_through_the_qobuz_route():
    html = _render("partials/queue_item.html", t={
        "id": 7, "title": "T", "artist": "A", "format": "m4a", "quality": "aac_256", "kind": "upgrade",
        "status": "error", "error_message": "boom", "route": "upgrade"})
    assert 'hx-post="/api/qobuz/upgrade/track/7"' in html
    assert 'hx-post="/api/qobuz/upgrade/dismiss/7"' in html
    assert "FLAC upgrade" in html


def test_failed_download_keeps_the_downloads_routes():
    html = _render("partials/queue_item.html", t={
        "id": 3, "title": "T", "artist": "A", "format": "m4a", "quality": "native", "kind": "download",
        "status": "error", "error_message": "boom", "route": "qobuz_first"})
    assert "/api/downloads/retry/3" in html and "/api/downloads/dismiss/3" in html
    assert "FLAC (Qobuz) or YouTube" in html


def _status(**qobuz) -> str:
    return _render("partials/qobuz_status.html", qobuz=qobuz, check={"state": "unchecked"},
                   error=None, notice=None)


def test_qobuz_card_states():
    assert "Not signed in" in _status(state="not_configured")
    connected = _status(state="connected", plan="Studio", hires=True, lossless=True,
                        max_quality_label="FLAC up to 24-bit / 192 kHz", downloads_today=3, daily_cap=100)
    for text in ("Connected", "Studio", "Hi-Res", "FLAC up to 24-bit / 192 kHz", "3 / 100 today"):
        assert text in connected
    assert "Sign in again" in _status(state="expired")
    assert "no lossless streaming" in _status(state="ineligible")


def test_qobuz_card_check_results():
    def card(state):
        return _render("partials/qobuz_status.html", qobuz={"state": "connected"},
                       check={"state": state, "error": "x"})
    assert "Check: PASS" in card("pass")
    assert "Check: FAIL" in card("fail")
    assert "Check: failed" in card("error")


def test_qobuz_card_warnings():
    assert "other accounts on this computer can read the token file" in _status(
        state="connected", file_protected=False)
    paused = _status(state="connected", breaker={"reason": "unavailable", "until": "2026-10-18T14:05:00+00:00"})
    assert "Qobuz paused until" in paused and 'datetime="2026-10-18T14:05:00+00:00"' in paused


def test_qobuz_token_field_never_carries_a_value():
    html = _status(state="connected")
    token = re.search(r'<input type="password"[^>]*>', html).group(0)
    assert 'name="user_auth_token"' in token and "value=" not in token


def test_sign_in_form_is_open_until_connected():
    assert "<details class=\"qobuz-signin\" open>" in _status(state="expired")
    assert "<details class=\"qobuz-signin\">" in _status(state="connected")


def _button(**context) -> str:
    base = {"scope": "album", "connected": True, "session_state": "connected", "eligible_ids": [],
            "eligible_count": 0, "queued_count": 0, "failed_count": 0, "message": None, "error": None}
    return _render("partials/qobuz_upgrade_button.html", **{**base, **context})


def test_upgrade_button_hint_when_signed_out():
    assert "Sign in to Qobuz on Home to upgrade to FLAC" in _button(connected=False)


def test_upgrade_button_renders_nothing_when_nothing_to_do():
    assert _button(unmatched_count=0).strip() == ""


def test_album_upgrade_button_posts_its_track_ids():
    html = _button(eligible_ids=[4, 5], eligible_count=2)
    assert "Upgrade to FLAC (2)" in html and 'hx-post="/api/qobuz/upgrade/tracks"' in html
    assert '"track_ids": "4,5"' in html and '"scope": "album"' in html


def test_all_upgrade_button_asks_first():
    html = _button(scope="all", eligible_count=12, queued_count=3)
    assert "Upgrade all eligible to FLAC (12)" in html and "hx-confirm=\"Queue 12 tracks" in html
    assert "3 upgrading" in html


def _library_row(**extra) -> str:
    row = {"id": 9, "title": "T", "artist": "A", "album": "B", "track_number": 1, "synced_at": None,
           "quality": "flac_24_96", "format": "flac", "is_lossless": True, **extra}
    return _render("partials/library_tracks.html", tracks=[row])


def test_library_offers_undo_only_while_revertible():
    assert 'hx-post="/api/qobuz/upgrade/revert/9"' in _library_row(upgrade_revertible=True)
    assert "Undo FLAC" not in _library_row(upgrade_revertible=False)


def test_sync_diff_lists_space_kept_and_superseded_copies():
    html = _render("partials/sync_diff.html", to_add=[], already_synced=[], on_device_only=[],
                   space={"needed_bytes": 3_200_000_000, "free_bytes": 1_100_000_000, "fits": False,
                          "too_large": ["A/B/huge.flac"], "too_long": []},
                   to_remove=[{"path": "A/B/01 - T.m4a", "title": "T", "artist": "A", "state": "waiting"}],
                   kept=[{"path": "A/B/02 - U.m4a", "title": "U", "artist": "A", "reason": "changed_on_card"}])
    assert "Needs 3.2 GB, 1.1 GB free" in html and "Not enough space on the card" in html
    assert "A/B/huge.flac" in html and "too large for the card" in html
    assert "1 superseded copy removed after its FLAC is copied" in html and "waits for the FLAC copy" in html
    assert "1 old copy kept" in html and "changed on the card — remove by hand if unwanted" in html


def test_upgrade_all_confirm_states_the_configured_keep_days_and_cap():
    html = _button(scope="all", eligible_count=2, keep_days=7, daily_cap=40)
    assert "kept 7 days so you can revert" in html and "Up to 40 a day." in html


def test_upgrade_all_confirm_promises_no_numbers_it_was_not_given():
    html = _button(scope="all", eligible_count=2)
    assert "the old file is kept so you can revert" in html and "a day." not in html


def test_library_upgrade_slot_refetches_when_the_panel_is_shown():
    html = (TEMPLATES / "panels" / "library.html").read_text(encoding="utf-8")
    slot = re.search(r'<div id="qobuz-upgrade-all-slot"[^>]*>', html).group(0)
    assert "leasure:panel-shown[detail.name=='library'] from:body" in slot and 'hx-swap="innerHTML"' in slot
