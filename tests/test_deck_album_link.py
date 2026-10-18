import pytest


def _serve_recent(monkeypatch, track):
    from routers import music

    base = {"id": "r1", "name": "Mr. Brightside", "artist": "The Killers", "duration_ms": 222_000,
            "image_url": "", "sources": [{"provider": "youtube", "id": "v1"}]}
    monkeypatch.setattr(music, "get_unified_recent", lambda limit, refresh: [{**base, **track}])


@pytest.mark.asyncio
async def test_recent_track_with_album_ref_opens_that_album(client, monkeypatch):
    _serve_recent(monkeypatch, {"album": "Hot Fuss", "album_ref": {"provider": "youtube", "id": "MPREb_hot"}})

    resp = await client.get("/api/music/recent")

    assert 'hx-get="/api/music/album/youtube/MPREb_hot"' in resp.text


@pytest.mark.asyncio
async def test_recent_track_without_album_ref_offers_find_album(client, monkeypatch):
    _serve_recent(monkeypatch, {"album": None, "album_ref": None})

    resp = await client.get("/api/music/deck")

    assert 'hx-get="/api/music/album/resolve"' in resp.text
    assert '"title": "Mr. Brightside"' in resp.text
    assert "Find album" in resp.text


@pytest.mark.asyncio
async def test_playlist_without_cover_has_an_empty_image(client, monkeypatch):
    from routers import music

    monkeypatch.setattr(music, "get_unified_playlists", lambda: [
        {"name": "Faker", "owner": "me", "provider": "spotify", "source_id": "p1", "image_url": None}])

    resp = await client.get("/api/music/playlists")

    assert 'data-image=""' in resp.text
    assert 'data-image="None"' not in resp.text
