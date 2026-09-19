import pytest
from sqlalchemy import select

FULL = {
    "spotify_uri": "spotify:track:abc",
    "youtube_id": "vid123",
    "isrc": "USABC1234567",
    "title": "Sing a Song",
    "artist": "Earth, Wind & Fire",
    "album": "Gratitude",
    "album_artist": "Earth, Wind & Fire",
    "genre": "soul, funk",
    "track_number": 3,
    "disc_number": 2,
    "total_tracks": 14,
    "total_discs": 2,
    "duration_ms": 189000,
    "year": 1975,
    "artwork_url": "https://example.com/art.jpg",
}


@pytest.mark.asyncio
async def test_upsert_track_sets_every_column_from_the_dict(db):
    from services.tracks import upsert_track

    async with db() as session:
        track = await upsert_track(session, FULL, quality="mp3_320", container="mp3", source="spotify")

    for field, value in FULL.items():
        assert getattr(track, field) == value, field
    assert (track.format, track.quality, track.source, track.status) == ("mp3", "mp3_320", "spotify", "pending")
    assert track.error_message is None


@pytest.mark.asyncio
async def test_upsert_track_keeps_the_row_and_requeues_it(db):
    from models import Track
    from services.tracks import upsert_track

    async with db() as session:
        first = await upsert_track(session, FULL, quality="mp3_320", container="mp3", source="spotify")
        first.status = "error"
        first.error_message = "boom"
        await session.commit()

        again = await upsert_track(session, {**FULL, "year": 1976}, quality="aac_256", container="m4a",
                                   source="spotify", existing=first)
        rows = (await session.execute(select(Track))).scalars().all()

    assert again.id == first.id
    assert len(rows) == 1
    assert (again.year, again.status, again.error_message) == (1976, "pending", None)


@pytest.mark.asyncio
async def test_upsert_track_keeps_metadata_a_later_dict_omits(db):
    """A re-queue from a thinner source must not wipe the year/disc an album fetch stored."""
    from services.tracks import upsert_track

    async with db() as session:
        track = await upsert_track(session, FULL, quality="mp3_320", container="mp3", source="spotify")
        track = await upsert_track(session, {"title": FULL["title"], "artist": FULL["artist"]},
                                   quality="mp3_320", container="mp3", source="spotify", existing=track)

    assert (track.year, track.disc_number, track.album_artist) == (1975, 2, "Earth, Wind & Fire")


@pytest.mark.asyncio
async def test_upsert_track_never_clears_the_other_providers_id(db):
    from services.tracks import upsert_track

    async with db() as session:
        track = await upsert_track(session, FULL, quality="mp3_320", container="mp3", source="spotify")
        track = await upsert_track(session, {**FULL, "youtube_id": ""}, quality="mp3_320", container="mp3",
                                   source="spotify", existing=track)

    assert track.youtube_id == "vid123"


@pytest.mark.asyncio
async def test_single_track_download_route_stores_year_disc_and_album_artist(client, db):
    from models import Track

    resp = await client.post("/api/music/download/track", data={
        "title": "Sing a Song", "artist": "Earth, Wind & Fire", "album": "Gratitude",
        "album_artist": "Earth, Wind & Fire", "youtube_id": "vid123",
        "year": "1975", "disc_number": "2", "total_tracks": "14", "format": "native",
    })
    assert resp.status_code == 200

    async with db() as session:
        track = await session.get(Track, resp.json()["track_id"])
    assert (track.album_artist, track.year, track.disc_number, track.total_tracks) == (
        "Earth, Wind & Fire", 1975, 2, 14)


@pytest.mark.asyncio
async def test_single_track_download_route_accepts_blank_optional_numbers(client, db):
    from models import Track

    resp = await client.post("/api/music/download/track", data={
        "title": "T", "artist": "A", "youtube_id": "abc", "format": "native",
        "year": "", "disc_number": "", "total_tracks": "",
    })
    assert resp.status_code == 200

    async with db() as session:
        track = await session.get(Track, resp.json()["track_id"])
    assert (track.year, track.disc_number, track.total_tracks) == (None, None, None)
