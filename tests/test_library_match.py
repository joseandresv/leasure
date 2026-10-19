from services import library_match
from services.album_merge import ef
from tests.album_fakes import seed


async def find(db, title, artists, duration_ms=None):
    async with db() as session:
        index = await library_match.song_index(session, {ef(title)})
        return index.find_song(title, set(artists), duration_ms)


async def test_a_song_without_an_album_is_found(db):
    track_id = await seed(db, album=None, album_artist=None)

    assert (await find(db, "Mr. Brightside", {"The Killers"})).id == track_id


async def test_a_song_on_another_album_is_found(db):
    track_id = await seed(db, album="Direct Hits")

    assert (await find(db, "Mr Brightside", {"The Killers, Someone"})).id == track_id


async def test_a_variant_is_another_song(db):
    await seed(db, title="Mr. Brightside (Live)")

    assert await find(db, "Mr. Brightside", {"The Killers"}) is None


async def test_durations_more_than_ten_seconds_apart_are_other_recordings(db):
    await seed(db, duration_ms=222_000)

    assert await find(db, "Mr. Brightside", {"The Killers"}, duration_ms=233_000) is None
    assert await find(db, "Mr. Brightside", {"The Killers"}, duration_ms=231_000) is not None


async def test_an_artist_spelling_from_a_sibling_row_finds_the_song(db):
    track_id = await seed(db, title="Dynamite", artist="방탄소년단", album_artist="방탄소년단", album=None)

    assert await find(db, "Dynamite", {"BTS"}) is None
    assert (await find(db, "Dynamite", {"BTS", "방탄소년단"})).id == track_id


async def test_a_done_row_wins_over_a_failed_one(db):
    await seed(db, status="error", album="A")
    done = await seed(db, album="B")

    assert (await find(db, "Mr. Brightside", {"The Killers"})).id == done
