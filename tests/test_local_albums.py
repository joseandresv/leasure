from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from models import AlbumInfo, Track, utc_now
from services import album_info, local_albums
from services.device import artist_folder
from services.local_albums import LOOSE_KEY, album_key, key_artist, norm_v2
from tests.album_fakes import seed

KILLERS = album_key("Hot Fuss", "The Killers", "The Killers")


def test_the_killers_album_keys_as_artist_and_album():
    assert (KILLERS.artist_norm, KILLERS.album_norm) == ("the killers", "hot fuss")
    assert KILLERS.key.startswith("a-") and len(KILLERS.key) == 22


@pytest.mark.parametrize("album, album_artist, artist", [
    ("Hot Fuss", None, "The Killers, Someone"),
    ("Hot Fuss", "The Killers, Someone", "The Killers, Someone"),
    ("HOT FUSS", "the killers", "the killers"),
    ("Hot Fuss [Explicit]", "The Killers", "The Killers"),
], ids=["no_album_artist", "legacy_row", "case", "explicit"])
def test_the_same_album_keys_the_same(album, album_artist, artist):
    assert album_key(album, album_artist, artist) == KILLERS


def test_spotify_and_youtube_spellings_of_a_collab_key_the_same():
    spotify = album_key("Watch the Throne", "JAY-Z, Kanye West", "JAY-Z, Kanye West, Frank Ocean")
    youtube = album_key("Watch the Throne", "JAY-Z", "JAY-Z")

    assert spotify == youtube and spotify.artist_norm == "jay z"


def test_a_remaster_suffix_keys_like_the_plain_album():
    assert album_key("A Night At The Opera (2011 Remaster)", "Queen", "Queen") == album_key(
        "A Night At The Opera", "Queen", "Queen")


@pytest.mark.parametrize("a, b", [
    (("Hot Fuss (Deluxe Edition)", "The Killers"), ("Hot Fuss", "The Killers")),
    (("1989 (Taylor's Version)", "Taylor Swift"), ("1989", "Taylor Swift")),
    (("Greatest Hits", "Queen"), ("Greatest Hits", "ABBA")),
], ids=["deluxe", "taylors_version", "same_title_other_artist"])
def test_different_albums_never_share_a_key(a, b):
    assert album_key(a[0], a[1], a[1]) != album_key(b[0], b[1], b[1])


def test_self_titled_albums_of_one_artist_are_one_group():
    assert album_key("Weezer", "Weezer", "Weezer") == album_key("weezer", None, "Weezer")


@pytest.mark.parametrize("text, expected", [("÷", "÷"), ("Remastered", "remastered"),
                                            ("A Night At The Opera (2011 Remaster)", "a night at the opera"),
                                            ("Album [Explicit]", "album"), ("Mr. Brightside", "mr brightside")])
def test_norm_v2_outputs_are_pinned(text, expected):
    assert norm_v2(text) == expected


def test_key_artist_is_pinned_and_never_empty():
    assert key_artist("Various Artists", "Various Artists") == "various artists"
    assert key_artist(None, None) == "unknown artist"
    assert album_key("Compilation", "Various Artists", "X") == album_key("Compilation", "Various Artists", "Y")


@pytest.mark.parametrize("artist, album_artist", [
    ("The Killers", "The Killers"), ("The Killers, Someone", None), ("The Killers, Someone", "The Killers, Someone"),
    ("JAY-Z, Kanye West, Frank Ocean", "JAY-Z, Kanye West"), ("Ed Sheeran", None), ("X", "Various Artists")])
def test_key_artist_follows_the_card_folder_rule(artist, album_artist):
    assert key_artist(artist, album_artist) == norm_v2(artist_folder(artist, album_artist).split(",")[0])


@pytest.mark.parametrize("album", ["", None, "Unknown Album", "unknown"])
def test_album_less_rows_have_no_key(album):
    assert album_key(album, "A", "A") is None
    assert local_albums.key_of(album, "A", "A") == LOOSE_KEY


def test_no_artist_at_all_does_not_crash():
    assert album_key("÷", None, None).artist_norm == "unknown artist"


async def test_every_group_key_round_trips_from_its_display_names(db):
    await seed(db, album="Hot Fuss", album_artist=None, artist="The Killers, Someone")
    await seed(db, title="Smile", album="Hot Fuss", album_artist="The Killers, Someone", artist="The Killers, Someone")
    await seed(db, title="Otis", album="Watch the Throne", album_artist="JAY-Z, Kanye West", artist="JAY-Z, Kanye West")
    await seed(db, title="Niggas", album="Watch the Throne", album_artist="JAY-Z", artist="JAY-Z")
    await seed(db, title="Perfect", album="÷", album_artist=None, artist="Ed Sheeran")
    await seed(db, title="Song", album="Compilation", album_artist="Various Artists", artist="X")

    async with db() as session:
        groups = await local_albums.scan(session)

    assert len(groups) == 4
    for group in groups:
        assert album_key(group.name, group.artist, group.artist).key == group.key


async def test_display_values_come_from_the_most_common_spelling(db):
    early, late = datetime(2026, 1, 1), datetime(2026, 2, 1)
    await seed(db, title="A", album="Hot Fuss", track_number=1, year=2004, total_tracks=11, downloaded_at=late)
    await seed(db, title="B", album="HOT FUSS", track_number=2, year=2004, total_tracks=11, downloaded_at=early)
    await seed(db, title="C", album="Hot Fuss", track_number=3, disc_number=2, year=None, total_tracks=13)

    async with db() as session:
        (group,) = await local_albums.scan(session)

    assert (group.name, group.artist, group.year, group.total_tracks) == ("Hot Fuss", "The Killers", 2004, 11)
    assert group.total_tracks_values == {11, 13} and group.total_discs == 2 and group.track_count == 3


async def test_album_less_rows_form_one_loose_group(db):
    await seed(db, title="One", album=None)
    await seed(db, title="Two", album="Unknown Album")

    async with db() as session:
        (group,) = await local_albums.scan(session)
        album, rows = await local_albums.load(session, LOOSE_KEY)

    assert (group.key, group.kind, group.name) == (LOOSE_KEY, "loose", "Loose songs")
    assert [r.title for r in rows] == ["One", "Two"]


async def test_quality_mix_counts_done_rows_only(db):
    await seed(db, title="A", quality="flac_16_44")
    await seed(db, title="B", quality="aac_256")
    await seed(db, title="C", quality="aac_256")
    await seed(db, title="D", quality="native", status="pending")

    async with db() as session:
        (group,) = await local_albums.scan(session)
        album, rows = await local_albums.load(session, KILLERS.key)

    assert group.quality_mix == [{"label": "AAC 256", "count": 2}, {"label": "FLAC", "count": 1}]
    assert len(rows) == 4 and album.track_count == 3


@pytest.mark.parametrize("tier, label", [("flac_24_96", "FLAC"), ("aac_256", "AAC 256"), ("mp3_320", "MP3 320"),
                                         ("opus_160", "Opus"), ("weird", "WEIRD")])
def test_quality_labels(tier, label):
    assert local_albums.quality_label(tier) == label


async def test_a_group_with_no_done_row_does_not_load(db):
    await seed(db, status="pending")

    async with db() as session:
        assert await local_albums.load(session, KILLERS.key) is None
        assert await local_albums.load(session, "not-a-key") is None


async def test_the_scan_cache_follows_inserts_updates_and_deletes(db):
    track_id = await seed(db)
    async with db() as session:
        assert [g.name for g in await local_albums.scan(session)] == ["Hot Fuss"]
    await seed(db, title="Somebody Told Me", album="Sam's Town")
    async with db() as session:
        assert len(await local_albums.scan(session)) == 2
        await session.execute(update(Track).where(Track.id == track_id).values(album="Hot Fuss (Deluxe Edition)"))
        await session.commit()
        assert {g.name for g in await local_albums.scan(session)} == {"Hot Fuss (Deluxe Edition)", "Sam's Town"}
        await session.execute(delete(Track).where(Track.id == track_id))
        await session.commit()
        assert [g.name for g in await local_albums.scan(session)] == ["Sam's Town"]


async def test_the_scan_cache_follows_a_same_length_rename(db):
    track_id = await seed(db, artist="Bjork", album_artist="Bjork", album="Debut")
    async with db() as session:
        before = {g.key for g in await local_albums.scan(session)}
        await session.execute(update(Track).where(Track.id == track_id).values(album_artist="Björk"))
        await session.commit()
        after = {g.key for g in await local_albums.scan(session)}

    assert after == {local_albums.key_of("Debut", "Björk", "Bjork")} and after != before


async def _keys(db, name, artist):
    async with db() as session:
        return await local_albums.find_key(session, name, artist)


async def test_find_key_matches_the_exact_album(db):
    await seed(db)

    assert await _keys(db, "Hot Fuss", "The Killers, Someone") == (KILLERS.key, True)


async def test_find_key_opens_the_standard_album_for_a_deluxe_edition(db):
    await seed(db)

    assert await _keys(db, "Hot Fuss (Deluxe Edition)", "The Killers") == (KILLERS.key, False)


@pytest.mark.parametrize("have, wanted", [("Greatest Hits", "Greatest Hits II"), ("Live", "Live at Wembley")])
async def test_find_key_never_opens_a_different_album(db, have, wanted):
    await seed(db, album=have, album_artist="Queen", artist="Queen")

    assert await _keys(db, wanted, "Queen") is None


async def test_find_key_gives_up_when_two_albums_fit(db):
    await seed(db, album="Hot Fuss (Deluxe Edition)")
    await seed(db, title="Smile", album="Hot Fuss (Bonus Tracks)")

    assert await _keys(db, "Hot Fuss", "The Killers") is None


async def test_a_stale_key_version_is_rekeyed_without_a_lookup(db):
    await seed(db)
    async with db() as session:
        session.add(AlbumInfo(album_key="a-00000000000000000000", key_version="1", key_album_raw="Hot Fuss",
                              key_artist_raw="The Killers", status="not_found", fetched_at=utc_now(),
                              retry_at=utc_now() + timedelta(days=7)))
        await session.commit()
        assert await album_info.rekey_stale_rows(session) == 1
        row = await session.scalar(select(AlbumInfo))

    assert (row.album_key, row.key_version) == (KILLERS.key, local_albums.KEY_VERSION)


async def test_rekeying_onto_a_newer_row_drops_the_older_one(db):
    async with db() as session:
        session.add(AlbumInfo(album_key="a-00000000000000000000", key_version="1", key_album_raw="Hot Fuss",
                              key_artist_raw="The Killers", status="not_found", fetched_at=utc_now() - timedelta(days=1)))
        session.add(AlbumInfo(album_key=KILLERS.key, key_version=local_albums.KEY_VERSION, key_album_raw="Hot Fuss",
                              key_artist_raw="The Killers", status="found", fetched_at=utc_now()))
        await session.commit()
        await album_info.rekey_stale_rows(session)
        rows = (await session.scalars(select(AlbumInfo))).all()

    assert [(r.album_key, r.status) for r in rows] == [(KILLERS.key, "found")]
