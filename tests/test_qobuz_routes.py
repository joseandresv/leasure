import json
from pathlib import Path

import pytest

from services import qobuz_client, qobuz_engine, qobuz_session
from services.yt_engine import DownloadFailed
from tests.qobuz_fakes import TOKEN, get_track, seed_track, store_session

HTMX = {"HX-Request": "true"}
needs_card = pytest.mark.skipif(not Path("templates/partials/qobuz_status.html").exists(),
                                reason="Qobuz card template not merged")
needs_button = pytest.mark.skipif(not Path("templates/partials/qobuz_upgrade_button.html").exists(),
                                  reason="upgrade button template not merged")


@pytest.fixture
def signed_in(monkeypatch):
    def fake_sign_in(user_id, token, source="paste"):
        store_session()
        return qobuz_session.status()

    monkeypatch.setattr(qobuz_session, "sign_in", fake_sign_in)


async def test_status_never_shows_the_token(client):
    store_session()

    resp = await client.get("/api/qobuz/status")

    assert resp.json()["state"] == "connected" and TOKEN not in resp.text


async def test_a_malformed_sign_in_is_refused_without_echoing_the_token(client):
    resp = await client.post("/api/qobuz/session", data={"user_id": "abc", "user_auth_token": TOKEN})

    assert resp.status_code == 400 and TOKEN not in resp.text


async def test_a_refused_token_answers_401(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise qobuz_client.QobuzAuthError("refused")

    monkeypatch.setattr(qobuz_session, "sign_in", refuse)

    resp = await client.post("/api/qobuz/session", data={"user_id": "123", "user_auth_token": TOKEN})

    assert resp.status_code == 401 and "copy it again" in resp.json()["detail"]


async def test_a_qobuz_outage_at_sign_in_answers_502(client, monkeypatch):
    def outage(*args, **kwargs):
        raise qobuz_client.QobuzNetworkError("Could not reach Qobuz — check the network connection.")

    monkeypatch.setattr(qobuz_session, "sign_in", outage)

    resp = await client.post("/api/qobuz/session", data={"user_id": "123", "user_auth_token": TOKEN})

    assert resp.status_code == 502 and "Could not reach Qobuz" in resp.json()["detail"]


async def test_a_good_sign_in_answers_its_status(client, signed_in):
    resp = await client.post("/api/qobuz/session", data={"user_id": "12344321", "user_auth_token": TOKEN})

    assert resp.status_code == 200 and resp.json()["status"]["state"] == "connected"


async def test_sign_out_forgets_the_token(client):
    store_session()

    resp = await client.delete("/api/qobuz/session")

    assert resp.json() == {"ok": True, "removed": True}
    assert qobuz_session.load() is None


async def test_check_runs_the_check(client, monkeypatch):
    monkeypatch.setattr(qobuz_session, "run_check", lambda: {"state": "pass", "checked_at": None})

    assert (await client.post("/api/qobuz/check")).json()["state"] == "pass"


async def test_a_cross_site_sign_out_is_rejected(client):
    store_session()

    resp = await client.delete("/api/qobuz/session", headers={"Sec-Fetch-Site": "cross-site"})

    assert resp.status_code == 403 and qobuz_session.load() is not None


async def test_upgrades_need_a_qobuz_sign_in(client, db):
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a")

    resp = await client.post("/api/qobuz/upgrade/tracks", data={"track_ids": str(track_id)})

    assert resp.status_code == 409 and resp.json()["detail"] == "Sign in to Qobuz first."


async def test_upgrade_tracks_queues_eligible_and_names_the_rest(client, db):
    store_session()
    eligible = await seed_track(db, status="done", file_path="/l/x.m4a")
    lossless = await seed_track(db, status="done", file_path="/l/y.flac", is_lossless=True)

    resp = await client.post("/api/qobuz/upgrade/tracks", data={"track_ids": f"{eligible},{lossless}"})

    assert resp.json() == {"queued": [eligible], "skipped": {str(lossless): "lossless"}}
    assert (await get_track(db, eligible)).upgrade_state == "queued"


async def test_bad_track_ids_answer_400(client):
    store_session()

    resp = await client.post("/api/qobuz/upgrade/tracks", data={"track_ids": "1,abc"})

    assert resp.status_code == 400


async def test_dismiss_clears_a_failed_upgrade(client, db):
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="error",
                                upgrade_error="No exact Qobuz match; the current file is kept.")

    assert (await client.post(f"/api/qobuz/upgrade/dismiss/{track_id}")).json() == {"dismissed": True}

    track = await get_track(db, track_id)
    assert (track.upgrade_state, track.upgrade_error, track.status) == (None, None, "done")


async def test_revert_answers_200_or_409(client, db, monkeypatch):
    track_id = await seed_track(db, status="done", file_path="/l/x.flac")
    outcomes = [Path("/l/x.m4a"), DownloadFailed(qobuz_engine.NOT_REVERTIBLE)]

    async def fake_revert(_track_id):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(qobuz_engine, "revert_upgrade", fake_revert)

    assert (await client.post(f"/api/qobuz/upgrade/revert/{track_id}")).json() == {"reverted": True}
    refused = await client.post(f"/api/qobuz/upgrade/revert/{track_id}")
    assert refused.status_code == 409 and refused.json()["detail"] == "There is no earlier file to go back to."


async def test_an_htmx_revert_without_a_backup_replaces_the_undo_button(client, db, monkeypatch):
    track_id = await seed_track(db, status="done", file_path="/l/x.flac")

    async def fake_revert(_track_id):
        raise DownloadFailed(qobuz_engine.NOT_REVERTIBLE)

    monkeypatch.setattr(qobuz_engine, "revert_upgrade", fake_revert)

    resp = await client.post(f"/api/qobuz/upgrade/revert/{track_id}", headers=HTMX)

    assert resp.status_code == 200 and "no earlier file" in resp.text and "<button" not in resp.text


async def test_the_upgrade_summary_counts_from_the_database(client, db):
    await seed_track(db, status="done", file_path="/l/x.m4a")
    await seed_track(db, status="done", file_path="/l/y.m4a", qobuz_skip_reason="no_match")
    await seed_track(db, status="done", file_path="/l/z.m4a", upgrade_state="error")

    summary = (await client.get("/api/qobuz/upgrade/summary")).json()

    assert summary == {"connected": False, "eligible": 2, "eligible_unmatched": 3, "queued": 0, "running": 0,
                       "failed": 1}


async def test_an_upgrade_shows_in_the_download_queue(client, db):
    store_session()
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="queued")
    await seed_track(db, quality="flac_lossless")

    rows = (await client.get("/api/downloads/queue")).json()

    assert [(r["kind"], r["status"], r["route"]) for r in rows["tracks"]] == [
        ("upgrade", "pending", "upgrade"), ("download", "pending", "qobuz_only")]
    assert rows["tracks"][0]["id"] == track_id and rows["active"] is True


@needs_card
@pytest.mark.parametrize("state, marker", [
    (None, "Not signed in"),
    ("connected", "Connected"),
    ("expired", "Sign in again"),
    ("ineligible", "no lossless streaming"),
])
async def test_the_card_names_the_session_state(client, state, marker):
    if state:
        store_session(state=state, plan="Studio", hires=True)

    html = (await client.get("/api/qobuz/status/html")).text

    assert marker in html and TOKEN not in html


@needs_card
async def test_the_card_warns_about_an_unprotected_token_file(client, monkeypatch):
    monkeypatch.setattr(qobuz_session.secret_file, "write_secret", lambda path, text: path.write_text(text(False)) and False)
    store_session()

    html = (await client.get("/api/qobuz/status/html")).text

    assert "other accounts on this computer can read the token file" in html


@needs_card
async def test_sign_out_from_the_card_shows_the_notice(client):
    store_session()

    html = (await client.delete("/api/qobuz/session", headers=HTMX)).text

    assert "Leasure forgot this token" in html and "Not signed in" in html


@needs_card
async def test_a_malformed_sign_in_on_the_card_shows_the_error(client):
    resp = await client.post("/api/qobuz/session", data={"user_id": "x", "user_auth_token": TOKEN}, headers=HTMX)

    assert resp.status_code == 200 and "User ID" in resp.text and TOKEN not in resp.text


@needs_button
async def test_the_album_button_counts_eligible_tracks(client, db):
    store_session()
    ids = [await seed_track(db, status="done", file_path=f"/l/{i}.m4a") for i in range(2)]

    html = (await client.get(f"/api/qobuz/upgrade/button/html?scope=album&track_ids={ids[0]},{ids[1]}")).text

    assert "Upgrade to FLAC (2)" in html


@needs_button
async def test_the_button_asks_to_sign_in_first(client, db):
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a")

    html = (await client.get(f"/api/qobuz/upgrade/button/html?scope=album&track_ids={track_id}")).text

    assert "Sign in to Qobuz on Home to upgrade to FLAC" in html


@needs_button
async def test_upgrade_all_confirm_names_the_configured_keep_days_and_cap(client, db, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "qobuz_upgrade_keep_days", 9)
    monkeypatch.setattr(settings, "qobuz_max_downloads_per_day", 42)
    store_session()
    await seed_track(db, status="done", file_path="/l/x.m4a")

    html = (await client.get("/api/qobuz/upgrade/button/html?scope=all")).text

    assert "kept 9 days so you can revert" in html and "Up to 42 a day." in html


@pytest.fixture
def rendered(monkeypatch):
    """The context of every template routers.qobuz renders, newest last."""
    import routers.qobuz as qobuz_router

    contexts = []
    original = qobuz_router.templates.TemplateResponse

    def capture(*args, **kwargs):
        contexts.append(kwargs["context"])
        return original(*args, **kwargs)

    monkeypatch.setattr(qobuz_router.templates, "TemplateResponse", capture)
    return contexts


def _queued_ids(resp) -> list[int]:
    return json.loads(resp.headers["HX-Trigger"])["qobuz:queued"]["track_ids"]


@pytest.mark.parametrize("route", ["tracks", "all", "track"])
async def test_queueing_upgrades_names_the_queued_rows_in_hx_trigger(client, db, route):
    store_session()
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", upgrade_state="error",
                                upgrade_error="FLAC upgrade failed.")
    url, data = {"tracks": ("/api/qobuz/upgrade/tracks", {"track_ids": str(track_id)}),
                 "all": ("/api/qobuz/upgrade/all", {}),
                 "track": (f"/api/qobuz/upgrade/track/{track_id}", {})}[route]

    resp = await client.post(url, data=data, headers=HTMX)

    assert resp.status_code == 200 and _queued_ids(resp) == [track_id]


async def test_a_revert_names_the_reverted_row_in_hx_trigger(client, db, monkeypatch):
    track_id = await seed_track(db, status="done", file_path="/l/x.flac")

    async def fake_revert(_track_id):
        return Path("/l/x.m4a")

    monkeypatch.setattr(qobuz_engine, "revert_upgrade", fake_revert)

    resp = await client.post(f"/api/qobuz/upgrade/revert/{track_id}", headers=HTMX)

    assert _queued_ids(resp) == [track_id]


async def test_sign_in_and_sign_out_announce_the_session_change(client, signed_in):
    signed = await client.post("/api/qobuz/session", data={"user_id": "12344321", "user_auth_token": TOKEN},
                               headers=HTMX)
    signed_out = await client.delete("/api/qobuz/session", headers=HTMX)

    assert signed.headers.get("HX-Trigger") == signed_out.headers.get("HX-Trigger") == "qobuz:changed"


async def test_upgrade_tracks_can_retry_tracks_qobuz_did_not_match(client, db):
    store_session()
    track_id = await seed_track(db, status="done", file_path="/l/x.m4a", qobuz_skip_reason="no_match")

    resp = await client.post("/api/qobuz/upgrade/tracks", data={"track_ids": str(track_id), "include_unmatched": "true"})

    assert resp.json()["queued"] == [track_id]


@needs_button
async def test_the_album_button_counts_tracks_not_matched_before(client, db, rendered):
    store_session()
    unmatched = await seed_track(db, status="done", file_path="/l/x.m4a", qobuz_skip_reason="no_match")

    await client.get(f"/api/qobuz/upgrade/button/html?scope=album&track_ids={unmatched}")

    context = rendered[-1]
    assert (context["eligible_count"], context["unmatched_count"], context["unmatched_ids"]) == (0, 1, [unmatched])


@needs_card
async def test_a_refused_sign_in_keeps_the_user_id_but_never_a_pasted_token(client, rendered, monkeypatch):
    def refuse(*args, **kwargs):
        raise qobuz_client.QobuzAuthError("refused")

    monkeypatch.setattr(qobuz_session, "sign_in", refuse)

    await client.post("/api/qobuz/session", data={"user_id": " 123 ", "user_auth_token": TOKEN}, headers=HTMX)
    await client.post("/api/qobuz/session", data={"user_id": TOKEN, "user_auth_token": TOKEN}, headers=HTMX)

    assert [c["user_id_value"] for c in rendered] == ["123", ""]


@needs_card
async def test_the_card_flags_an_expired_sign_in(client, rendered):
    store_session(state="expired")

    await client.get("/api/qobuz/status/html")

    assert rendered[-1]["expired"] is True


async def test_a_refused_sign_in_logs_the_reason_without_the_token(client, monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise qobuz_client.QobuzAuthError(f"refused {TOKEN}")

    monkeypatch.setattr(qobuz_session, "sign_in", refuse)

    await client.post("/api/qobuz/session", data={"user_id": "123", "user_auth_token": TOKEN})

    assert "Qobuz sign-in failed (401)" in caplog.text and TOKEN not in caplog.text


async def test_a_failed_check_logs_the_reason(client, monkeypatch, caplog):
    monkeypatch.setattr(qobuz_session, "run_check",
                        lambda: {"state": "error", "checked_at": None, "error": "Could not reach Qobuz."})

    await client.post("/api/qobuz/check")

    assert "Qobuz check error: Could not reach Qobuz." in caplog.text
