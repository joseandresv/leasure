"""/api/saved download routes and the Downloads queue with a batch: POST-only, read-only previews, a stale
selection queues nothing, and a long batch shows as one summary row that does not keep the launcher waiting."""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from models import Track
from models_saved import SavedBatch, SavedBatchItem
from services import saved_download, yt_engine
from tests.qobuz_fakes import store_session
from tests.saved_fakes import add_track, seed_collection
from tests.saved_fakes import saved_song as song
from tests.saved_fakes import seed_saved as seed


@pytest.fixture(autouse=True)
def room(monkeypatch):
    store_session()
    monkeypatch.setattr(yt_engine, "downloads_today", lambda: 0)
    monkeypatch.setattr(saved_download.shutil, "disk_usage", lambda _path: SimpleNamespace(free=500 * 1000**3))


async def count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count(model.id)))


async def test_a_saved_song_downloads_by_its_id_only(client, db):
    [item] = await seed(db, "spotify", "track", [song(1)])

    first = await client.post(f"/api/saved/download/item/{item.id}", data={"format": "native"})
    second = await client.post(f"/api/saved/download/item/{item.id}", data={"format": "native"})

    assert first.json()["status"] == "queued" and second.json()["status"] == "already_queued"
    assert await count(db, Track) == 1


async def test_a_song_download_answers_htmx_with_the_badge_and_an_event(client, db):
    [item] = await seed(db, "spotify", "track", [song(1)])

    response = await client.post(f"/api/saved/download/item/{item.id}", data={"format": "mp3"},
                                 headers={"HX-Request": "true"})

    assert "Queued" in response.text and response.headers["HX-Trigger"] == "saved:queued"


async def test_a_big_album_answers_with_the_confirmation_and_queues_nothing(client, db):
    item = await seed_collection(db, "spotify", "album", [song(n) for n in range(51)])

    body = (await client.post(f"/api/saved/download/item/{item.id}", data={"format": "native"})).json()

    assert body["confirm"]["songs"] == 51 and body["confirm"]["selection"] and await count(db, Track) == 0


async def test_a_small_album_queues_its_missing_songs(client, db):
    await add_track(db, title="Song 0", artist="Artist", album="Album", spotify_uri="spotify:track:t0")
    item = await seed_collection(db, "spotify", "album", [song(n) for n in range(3)])

    body = (await client.post(f"/api/saved/download/item/{item.id}", data={"format": "native"})).json()

    assert body["queued"] == 2 and body["skipped"] == {"library": 1}


async def test_an_artist_has_no_download(client, db):
    [item] = await seed(db, "spotify", "artist", [song(1)])

    assert (await client.post(f"/api/saved/download/item/{item.id}", data={"format": "native"})).status_code == 400
    assert (await client.get("/api/saved/download/preview", params={"kind": "artist"})).status_code == 400


async def test_an_unknown_format_is_a_400(client, db):
    [item] = await seed(db, "spotify", "track", [song(1)])

    assert (await client.post(f"/api/saved/download/item/{item.id}", data={"format": "flac"})).status_code == 400


async def test_the_preview_is_read_only_and_the_confirm_creates_the_batch(client, db):
    await seed(db, "spotify", "track", [song(n) for n in range(4)])

    preview = (await client.get("/api/saved/download/preview", params={"format": "native"})).json()["confirm"]
    assert (preview["total"], preview["days_at_least"], preview["refused"]) == (4, 1, None)
    assert await count(db, SavedBatch) == 0 and await count(db, Track) == 0

    response = await client.post("/api/saved/download/all", data={"format": "native", "selection": preview["selection"]})

    assert response.status_code == 200 and response.headers["HX-Trigger"] == "saved:queued"
    assert response.json()["batches"][0]["waiting"] == 4 and await count(db, Track) == 0


async def test_a_stale_selection_is_answered_with_new_numbers(client, db):
    await seed(db, "spotify", "track", [song(1)])

    response = await client.post("/api/saved/download/all", data={"format": "native", "selection": "old"})

    assert response.status_code == 409 and response.json()["confirm"]["stale"] is True
    assert await count(db, SavedBatch) == 0


async def test_download_all_is_post_only_and_same_site(client, db):
    await seed(db, "spotify", "track", [song(1)])
    token = (await client.get("/api/saved/download/preview")).json()["confirm"]["selection"]

    assert (await client.get("/api/saved/download/all")).status_code == 405
    cross = await client.post("/api/saved/download/all", data={"format": "native", "selection": token},
                              headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 403 and await count(db, SavedBatch) == 0


async def make_batch(client, **params) -> int:
    token = (await client.get("/api/saved/download/preview", params=params)).json()["confirm"]["selection"]
    body = (await client.post("/api/saved/download/all", data={**params, "selection": token})).json()
    return body["batches"][-1]["id"]


async def test_a_batch_pauses_resumes_and_cancels(client, db):
    await seed(db, "spotify", "track", [song(1)])
    batch_id = await make_batch(client)

    assert (await client.post(f"/api/saved/batches/{batch_id}/pause")).json()["batches"][0]["state"] == "paused"
    assert (await client.post(f"/api/saved/batches/{batch_id}/pause")).status_code == 409
    assert (await client.post(f"/api/saved/batches/{batch_id}/resume")).json()["batches"][0]["state"] == "active"
    assert (await client.post(f"/api/saved/batches/{batch_id}/cancel")).json()["batches"] == []
    assert (await client.post("/api/saved/batches/999/cancel")).status_code == 404


async def test_failed_songs_are_listed_with_the_batch_and_can_be_retried(client, db):
    await seed(db, "spotify", "track", [song(1)])
    batch_id = await make_batch(client)
    failed = await add_track(db, title="Song 1", artist="Artist", album="Album", spotify_uri="spotify:track:t1",
                             status="error", error_message="No match")
    async with db() as session:
        item = await session.scalar(select(SavedBatchItem))
        item.state, item.track_id, item.note = "error", failed.id, "No match"
        await session.commit()

    batches = (await client.get("/api/saved/batches")).json()["batches"]
    queue = (await client.get("/api/downloads/queue")).json()

    assert batches[0]["failures"]["by_reason"] == [{"reason": "No match", "count": 1}]
    assert queue["tracks"] == []
    retried = (await client.post(f"/api/saved/batches/{batch_id}/retry-failed")).json()["batches"][0]
    assert (retried["failed"], retried["waiting"]) == (0, 1)


async def test_a_long_batch_is_one_summary_row_and_does_not_keep_the_launcher_waiting(client, db):
    await seed(db, "spotify", "track", [song(n) for n in range(300)])
    await make_batch(client)
    fed = await add_track(db, title="Song 0", artist="Artist", album="Album", status="pending")
    async with db() as session:
        item = await session.scalar(select(SavedBatchItem).order_by(SavedBatchItem.position))
        item.state, item.track_id = "fed", fed.id
        await session.commit()

    queue = (await client.get("/api/downloads/queue")).json()

    assert [t["bulk"] for t in queue["tracks"]] == [True] and len(queue["batches"]) == 1
    assert queue["batches"][0]["waiting"] == 299 and queue["active"] is False


async def test_the_list_marks_songs_waiting_in_a_batch(client, db):
    await seed(db, "spotify", "track", [song(1), song(2)])
    await make_batch(client, q="song 1")

    items = (await client.get("/api/saved/list")).json()["items"]

    assert {i["title"]: i["in_batch"] for i in items} == {"Song 1": True, "Song 2": False}
