"""The Saved list: merging the same song across services, filters, sorts, paging and library state."""
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta

from db import DB_PATH
from models import Track, utc_now
from models_saved import SavedItem
from services import library_match, local_albums, saved_listing, saved_match
from services.saved_listing import ListFilters
from services.saved_store import SavedData, upsert_items
from tests.saved_fakes import add_track


def song(source_id: str, title: str, artist: str = "Artist", *, day: int = 1, album: str = "Album", **extra) -> SavedData:
    return SavedData(source_id=source_id, title=title, artist=artist, album=album,
                     added_at=datetime(2026, 9, day, 10), **extra)


async def seed(db, source: str, rows: list[SavedData], kind: str = "track") -> None:
    async with db() as session:
        await upsert_items(session, source, kind, rows, now=utc_now())
        await session.commit()


async def page(db, **filters) -> dict:
    async with db() as session:
        return await saved_listing.list_page(session, ListFilters.parse(**filters))


async def test_the_same_song_on_two_services_shows_once_with_both_sources_and_the_newest_date(db):
    await seed(db, "spotify", [song("t1", "Mr. Brightside - Remastered 2011", "The Killers", day=3)])
    await seed(db, "qobuz", [song("q1", "Mr. Brightside", "The Killers", day=9), song("q2", "Other", day=5)])
    result = await page(db)
    assert result["total"] == 2
    first = result["items"][0]
    assert first["title"] == "Mr. Brightside" and first["sources"] == ["qobuz", "spotify"]
    assert first["added_label"].startswith("9 Sep")


async def test_one_source_lists_every_item_of_it(db):
    await seed(db, "spotify", [song("t1", "Same"), song("t2", "Same")])
    assert (await page(db, source="spotify"))["total"] == 2
    assert (await page(db))["total"] == 1


async def test_sorts(db):
    await seed(db, "spotify", [song("t1", "Beta", "Zed", day=1), song("t2", "Alpha", "Ann", day=2),
                               song("t3", "Gamma", "Bob", day=3)])
    titles = {sort: [i["title"] for i in (await page(db, sort=sort))["items"]] for sort in saved_listing.SORTS}
    assert titles == {"added": ["Gamma", "Alpha", "Beta"], "added_asc": ["Beta", "Alpha", "Gamma"],
                      "artist": ["Alpha", "Gamma", "Beta"], "title": ["Alpha", "Beta", "Gamma"]}


async def test_the_page_is_clamped_and_positions_are_global(db):
    await seed(db, "spotify", [song(f"t{n}", f"Song {n}", day=1 + n % 20) for n in range(5)])
    result = await page(db, per=2, page=99)
    assert (result["page"], result["pages"], result["first"], result["last"]) == (3, 3, 5, 5)
    assert result["items"][0]["position"] == 5 and result["items"][0]["setsize"] == 5


async def test_the_filter_box_ignores_accents_and_case_and_escapes_wildcards(db):
    await seed(db, "spotify", [song("t1", "Halo", "Beyoncé"), song("t2", "100% Pure"), song("t3", "1000 Pure")])
    assert [i["title"] for i in (await page(db, q="BEYONCE halo"))["items"]] == ["Halo"]
    assert [i["title"] for i in (await page(db, q="100%"))["items"]] == ["100% Pure"]


async def test_removed_items_show_only_with_the_toggle(db):
    await seed(db, "spotify", [song("t1", "Gone"), song("t2", "Here")])
    async with db() as session:
        row = (await session.execute(SavedItem.__table__.select().where(SavedItem.source_id == "t1"))).one()
        item = await session.get(SavedItem, row.id)
        item.removed_at = utc_now()
        await session.commit()
    assert [i["title"] for i in (await page(db))["items"]] == ["Here"]
    shown = {i["title"]: i["removed"] for i in (await page(db, removed="1"))["items"]}
    assert shown == {"Gone": True, "Here": False}


async def test_the_page_is_matched_live_even_when_the_cache_is_stale(db):
    await seed(db, "spotify", [song("t1", "Song 1", uri="spotify:track:t1")])
    await add_track(db, spotify_uri="spotify:track:t1", status="done", synced_at=utc_now())
    item = (await page(db))["items"][0]
    assert item["library"]["state"] == "library" and item["library"]["synced"] is True
    assert item["library"]["quality"] == "mp3_320" and item["local_album_key"].startswith("a-")


async def test_a_song_on_another_album_counts_as_owned_unless_opted_in(db):
    await seed(db, "qobuz", [song("q1", "Song 1", album="Live At Home"), song("q2", "Song 2")])
    await add_track(db, title="Song 1", album="Studio Album")
    await saved_match.rematch_all(db)
    owned = await page(db, missing="1")
    assert [i["title"] for i in owned["items"]] == ["Song 2"] and owned["other_album"] == 1
    opted = await page(db, missing="1", other_album="1")
    assert {i["title"] for i in opted["items"]} == {"Song 1", "Song 2"}
    assert next(i for i in opted["items"] if i["title"] == "Song 1")["library"]["state"] == "other_album"


async def test_the_background_rematch_picks_up_a_new_download_and_queued_rows(db):
    await seed(db, "youtube", [song("v1", "Song 1"), song("v2", "Song 2")])
    await saved_match.rematch_all(db)
    assert (await page(db, missing="1"))["missing"] == 2
    await add_track(db, youtube_id="v1", status="done")
    await add_track(db, title="Song 2", youtube_id="v2", status="pending")
    await saved_match.rematch_all(db)
    counts = await page(db, missing="1")
    assert (counts["in_library"], counts["queued"], counts["missing"]) == (1, 1, 0)
    assert counts["counts_as_of"] is not None


async def test_qobuz_favorites_match_the_library_by_qobuz_id(db):
    await seed(db, "qobuz", [song("555", "Different Title")])
    await add_track(db, title="Something else", qobuz_id="555")
    assert (await page(db))["items"][0]["library"]["state"] == "library"


def test_library_index_finds_a_row_by_qobuz_id():
    row = type("Row", (), {"title": "X", "artist": "A", "album_artist": None, "album": "B", "status": "done",
                           "youtube_id": None, "spotify_uri": None, "isrc": None, "qobuz_id": "9"})()
    index = library_match.LibraryIndex([row])
    assert index.find(library_match.TrackKeys(title="Y", artist="Z", qobuz_id="9")) is row


async def test_a_saved_album_counts_its_tracks_already_in_the_library(db):
    await seed(db, "spotify", [SavedData(source_id="al1", title="Album", artist="Artist", track_count=2)], kind="album")
    await add_track(db, title="Song 1", track_number=1)
    await add_track(db, title="Song 2", track_number=2)
    await saved_match.rematch_all(db)
    item = (await page(db, kind="album", source="spotify"))["items"][0]
    assert (item["count_in_library"], item["track_count"]) == (2, 2) and item["local_album_key"].startswith("a-")
    assert (await page(db, kind="album", missing="1"))["total"] == 1
    assert (await page(db, kind="album", missing="1"))["items"] == []


async def test_list_page_cost_is_recorded(db, capsys):
    """Rematch timing, printed and not asserted (the design's 5,000 x 10,000 run is too slow for the suite)."""
    async with db() as session:
        session.add_all([SavedItem(source="spotify", kind="track", source_id=f"t{n}", title=f"Song {n}", artist="Artist",
                                   group_key=f"song:song {n}|artist", search_text=f"song {n} artist",
                                   added_at=utc_now() - timedelta(minutes=n)) for n in range(500)])
        session.add_all([Track(title=f"Song {n}", artist="Artist", album="Album", status="done") for n in range(1000)])
        await session.commit()
    started = time.perf_counter()
    await saved_match.rematch_all(db)
    print(f"rematch of 500 saved songs against 1,000 tracks: {time.perf_counter() - started:.2f}s")


def other_writer_can_lock() -> bool:
    with closing(sqlite3.connect(DB_PATH, timeout=0)) as other:
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        other.rollback()
        return True


async def test_the_album_rematch_holds_no_write_lock_while_it_matches(db, monkeypatch):
    await seed(db, "spotify", [SavedData(source_id=f"al{n}", title=f"Album {n}", artist="Artist", track_count=1)
                               for n in range(3)], kind="album")
    await add_track(db, title="Song 1", album="Album 1")
    find_key, free = local_albums.find_key, []

    async def probing(*args, **kwargs):
        found = await find_key(*args, **kwargs)
        free.append(other_writer_can_lock())
        return found

    monkeypatch.setattr(local_albums, "find_key", probing)
    await saved_match.rematch_all(db)
    assert free == [True, True, True]
