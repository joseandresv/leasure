"""/api/saved: read-only GETs over the snapshot, POST-only refreshes, and the item detail."""
import asyncio

import pytest

from models import utc_now
from services import qobuz_session, saved_sync
from services.saved_store import SavedData, upsert_items
from tests.qobuz_fakes import TOKEN
from tests.saved_fakes import FakeQobuzLibrary, add_track, qalbum

ITEM_KEYS = {"id", "source", "kind", "title", "artist", "album", "album_artist", "image_url", "duration_ms",
             "track_number", "disc_number", "year", "readable", "qobuz_hires", "qobuz_streamable", "added_at",
             "added_label", "sources", "members", "removed", "library", "in_batch", "count_in_library", "track_count",
             "owner_is_me", "local_album_key", "position", "setsize"}


class RecordedRuns(list):
    release: asyncio.Event


@pytest.fixture
def runs(monkeypatch):
    """Every refresh the routes start is recorded instead of reading a service; it runs until `release` is set."""
    calls = RecordedRuns()
    release = asyncio.Event()

    def runner(source):
        async def run(_factory, full):
            calls.append((source, full))
            await release.wait()
        return run

    for source in ("spotify", "qobuz", "youtube"):
        monkeypatch.setitem(saved_sync._RUNNERS, source, runner(source))
    calls.release = release
    return calls


async def seed(db, source, kind, rows):
    async with db() as session:
        await upsert_items(session, source, kind, rows, now=utc_now())
        await session.commit()


async def test_no_get_starts_a_refresh(client, runs):
    for path in ("/api/saved", "/api/saved/list", "/api/saved/status", "/api/saved/list?source=qobuz&kind=album"):
        assert (await client.get(path)).status_code == 200
    assert runs == []


async def test_the_list_json_carries_the_item_contract(client, db):
    await seed(db, "spotify", "track", [SavedData(source_id="t1", title="Song", artist="Artist", album="Album")])
    body = (await client.get("/api/saved/list", params={"q": "song", "per": 500, "page": 0})).json()
    assert body["total"] == 1 and body["per"] == 200 and body["page"] == 1
    assert set(body["items"][0]) == ITEM_KEYS
    assert body["filters"] == {"source": "all", "kind": "track", "missing": 0, "other_album": 0, "removed": 0,
                               "q": "song", "sort": "added", "page": 1, "per": 200}


async def test_unknown_filter_values_fall_back_to_defaults(client):
    body = (await client.get("/api/saved/list", params={"source": "tidal", "kind": "x", "sort": "bogus"})).json()
    assert (body["filters"]["source"], body["filters"]["kind"], body["filters"]["sort"]) == ("all", "track", "added")


async def test_a_refresh_starts_one_job_and_refuses_a_second_while_it_runs(client, runs):
    qobuz_session.save(qobuz_session.StoredSession(user_id="42", user_auth_token=TOKEN))
    first = await client.post("/api/saved/refresh", data={"source": "qobuz", "full": "1"})
    await asyncio.sleep(0)
    assert first.status_code == 200 and runs == [("qobuz", True)]
    second = await client.post("/api/saved/refresh", data={"source": "qobuz"})
    assert second.status_code == 409 and second.json()["reason"] == "running"
    runs.release.set()
    await saved_sync.saved_sync._jobs["qobuz"]
    await saved_sync.saved_sync._rematch_task


async def test_a_refresh_is_refused_while_the_source_is_not_connected(client, runs, monkeypatch, tmp_path):
    monkeypatch.setattr(saved_sync.youtube_client, "HEADERS_PATH", tmp_path / "missing.json")
    response = await client.post("/api/saved/refresh", data={"source": "youtube"})
    assert response.status_code == 409 and response.json()["reason"] == "not_connected" and runs == []


async def test_a_spotify_refresh_is_refused_while_rate_limited(client, runs, monkeypatch):
    monkeypatch.setattr(saved_sync.sp, "_rate_limited_until", saved_sync.sp.time.monotonic() + 3600)
    response = await client.post("/api/saved/refresh", data={"source": "spotify"})
    assert response.status_code == 409 and response.json()["reason"] == "rate_limited"
    assert response.json()["resume_after"] is not None and runs == []


async def test_an_unknown_source_is_a_400(client):
    assert (await client.post("/api/saved/refresh", data={"source": "tidal"})).status_code == 400


async def test_a_cross_site_refresh_is_rejected(client, runs):
    response = await client.post("/api/saved/refresh", data={"source": "qobuz"}, headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403 and runs == []


async def test_opening_a_qobuz_album_reads_its_tracklist_once(client, db, monkeypatch):
    qobuz_session.save(qobuz_session.StoredSession(user_id="42", user_auth_token=TOKEN))
    fake = FakeQobuzLibrary(albums=[qalbum(1)])
    monkeypatch.setattr(qobuz_session, "make_client", lambda _stored: fake)
    await seed(db, "qobuz", "album", [SavedData(source_id="qa1", title="Album 1", artist="Miles Davis", track_count=1)])
    item_id = (await client.get("/api/saved/list", params={"kind": "album"})).json()["items"][0]["id"]
    body = (await client.get(f"/api/saved/item/{item_id}")).json()
    assert [e["title"] for e in body["entries"]] == ["So What"] and body["missing"] == 1 and body["notice"] is None
    await client.get(f"/api/saved/item/{item_id}")
    assert fake.calls == ["get_album:qa1"]


async def test_a_qobuz_album_without_a_sign_in_shows_a_notice(client, db):
    await seed(db, "qobuz", "album", [SavedData(source_id="qa1", title="Album 1", artist="Miles Davis")])
    item_id = (await client.get("/api/saved/list", params={"kind": "album"})).json()["items"][0]["id"]
    body = (await client.get(f"/api/saved/item/{item_id}")).json()
    assert body["entries"] == [] and "sign-in" in body["notice"]


async def test_an_artist_detail_lists_their_saved_songs_on_every_service(client, db):
    await seed(db, "spotify", "artist", [SavedData(source_id="a1", title="Miles Davis")])
    await seed(db, "spotify", "track", [SavedData(source_id="t1", title="So What", artist="Miles Davis"),
                                        SavedData(source_id="t2", title="Other", artist="Someone")])
    await seed(db, "qobuz", "track", [SavedData(source_id="q1", title="Blue in Green", artist="Miles Davis, Bill Evans")])
    item_id = (await client.get("/api/saved/list", params={"kind": "artist"})).json()["items"][0]["id"]
    body = (await client.get(f"/api/saved/item/{item_id}")).json()
    assert {i["title"] for i in body["artist_items"]} == {"So What", "Blue in Green"}


async def test_an_album_partly_in_the_library_links_the_library_album(client, db):
    await seed(db, "spotify", "album", [SavedData(source_id="al1", title="Album", artist="Artist")])
    await add_track(db, title="Song 1")
    item_id = (await client.get("/api/saved/list", params={"kind": "album"})).json()["items"][0]["id"]
    body = (await client.get(f"/api/saved/item/{item_id}")).json()
    assert body["library_album_url"].startswith("/api/albums/a-")


async def test_a_missing_item_is_a_404(client):
    assert (await client.get("/api/saved/item/999999")).status_code == 404


async def test_status_lists_every_source_and_does_not_poll_when_idle(client):
    body = (await client.get("/api/saved/status")).json()
    assert [s["source"] for s in body["sources"]] == ["spotify", "qobuz", "youtube"]
    assert body["poll_s"] is None and body["sources"][0]["never_read"] is True
