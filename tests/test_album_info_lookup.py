import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from models import AlbumInfo, utc_now
from services import album_info, deezer, local_albums, musicbrainz
from services.deezer import DeezerUnavailable
from services.local_albums import album_key
from services.musicbrainz import MusicBrainzUnavailable
from tests.album_fakes import (
    HOT_FUSS,
    MBID,
    MBID_2,
    MBID_3,
    FakeAPI,
    deezer_album,
    deezer_hit,
    deezer_tracks,
    duration,
    mb_hit,
    mb_release,
    seed,
    seed_album,
)

KEY = album_key("Hot Fuss", "The Killers", "The Killers").key


@pytest.fixture(autouse=True)
def no_cover(monkeypatch):
    async def none(url):
        return None

    monkeypatch.setattr(album_info, "_fetch_cover", none)


@pytest.fixture
def mb(monkeypatch):
    api = FakeAPI({"/release/": {"releases": []}})
    monkeypatch.setattr(musicbrainz, "get_checked", api)
    return api


@pytest.fixture
def dz(monkeypatch):
    api = FakeAPI({"/search/album": {"data": []}})
    monkeypatch.setattr(deezer, "get_checked", api)
    return api


async def info_row(db, key=KEY) -> AlbumInfo | None:
    async with db() as session:
        return await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == key))


async def found_row(db, source_id=MBID, confidence=1.0, **fields):
    async with db() as session:
        session.add(AlbumInfo(album_key=KEY, key_version=local_albums.KEY_VERSION, key_album_raw="Hot Fuss",
                              key_artist_raw="The Killers", status="found", source="musicbrainz", source_id=source_id,
                              release_group_id="rg-hot", confidence=confidence, title="Hot Fuss",
                              artist="The Killers", tracks_json=json.dumps([]), fetched_at=utc_now(), **fields))
        await session.commit()


# --- MusicBrainz ---


async def test_musicbrainz_match_is_stored_with_its_tracklist(db, mb, dz):
    await seed_album(db)
    mb.routes["/release/"] = {"releases": [mb_hit()]}
    mb.routes[f"/release/{MBID}"] = mb_release()

    assert await album_info.lookup(KEY) == "found"

    search = dict(mb.calls)["/release/"]
    assert search == {"query": 'release:"Hot Fuss" AND artist:"The Killers"', "limit": 25}
    assert dict(mb.calls)[f"/release/{MBID}"] == {"inc": musicbrainz.RELEASE_INC}
    row = await info_row(db)
    assert (row.status, row.source, row.source_id, row.release_group_id) == ("found", "musicbrainz", MBID, "rg-hot")
    assert (row.label, row.catalog_number, row.barcode, row.release_date, row.year) == (
        "Island", "B0002468-02", "602498617373", "2004-06-07", 2004)
    assert (row.total_tracks, row.total_discs, row.media_format) == (11, 1, "Digital Media")
    assert row.cover_url == f"https://coverartarchive.org/release/{MBID}/front-500"
    assert row.confidence == pytest.approx(1.0) and row.retry_at is None
    tracks = json.loads(row.tracks_json)
    assert tracks[1] == {"disc": 1, "position": 2, "title": "Mr. Brightside", "artist": "The Killers",
                         "duration_ms": duration(1), "isrc": None, "source_track_id": "rec-0-1"}
    assert json.loads(row.genres_json) == ["alternative rock", "indie rock"]
    assert dz.calls == []


async def test_a_digital_release_is_picked_over_vinyl_and_cd(db, mb, dz):
    await seed_album(db)
    mb.routes["/release/"] = {"releases": [mb_hit(MBID, formats=('12" Vinyl',)), mb_hit(MBID_2, formats=("CD",)),
                                           mb_hit(MBID_3, formats=("Digital Media",))]}
    mb.routes[f"/release/{MBID_3}"] = mb_release(MBID_3)

    await album_info.lookup(KEY)

    assert [p for p in mb.paths() if p.startswith("/release/") and p != "/release/"] == [f"/release/{MBID_3}"]


async def test_the_standard_edition_is_picked_over_the_deluxe(db, mb, dz):
    await seed_album(db, total_tracks=11)
    mb.routes["/release/"] = {"releases": [
        mb_hit(MBID, title="Hot Fuss (Deluxe Edition)", track_count=18, rg_id="rg-deluxe"), mb_hit(MBID_2)]}
    mb.routes[f"/release/{MBID_2}"] = mb_release(MBID_2)

    await album_info.lookup(KEY)

    assert f"/release/{MBID}" not in mb.paths() and (await info_row(db)).source_id == MBID_2


async def test_the_release_in_the_librarys_script_comes_first(db, mb, dz):
    await seed_album(db)
    mb.routes["/release/"] = {"releases": [mb_hit(MBID, script="Jpan", country="JP"), mb_hit(MBID_2, script="Latn",
                                                                                              country="GB")]}
    mb.routes[f"/release/{MBID_2}"] = mb_release(MBID_2)

    await album_info.lookup(KEY)

    assert (await info_row(db)).source_id == MBID_2


async def test_the_second_search_drops_the_name_suffix(db, mb, dz):
    await seed_album(db, album="Hot Fuss - Deluxe Edition")
    key = album_key("Hot Fuss - Deluxe Edition", "The Killers", "The Killers").key
    mb.routes["/release/"] = lambda params: {"releases": [mb_hit()] if params["query"].startswith('release:"Hot Fuss"')
                                            else []}
    mb.routes[f"/release/{MBID}"] = mb_release()

    await album_info.lookup(key)

    queries = [params["query"] for path, params in mb.calls if path == "/release/"]
    assert queries == ['release:"Hot Fuss \\- Deluxe Edition" AND artist:"The Killers"',
                       'release:"Hot Fuss" AND artist:"The Killers"']
    assert (await info_row(db, key)).status == "found"


async def test_the_isrc_shortcut_prefers_the_release_with_every_isrc(db, mb, dz):
    await seed(db, title=HOT_FUSS[0], track_number=1, duration_ms=duration(0), isrc="USIR10400001")
    await seed(db, title=HOT_FUSS[1], track_number=2, duration_ms=duration(1), isrc="USIR10400002")
    await seed_album(db, titles=HOT_FUSS[2:], numbers=range(3, 12))
    mb.routes["/isrc/USIR10400001"] = {"recordings": [{"id": "r1", "releases": [{"id": MBID, "title": "Hot Fuss"},
                                                                                  {"id": MBID_2, "title": "Hot Fuss"}]}]}
    mb.routes["/isrc/USIR10400002"] = {"recordings": [{"id": "r2", "releases": [{"id": MBID_2, "title": "Hot Fuss"}]}]}
    mb.routes[f"/release/{MBID_2}"] = mb_release(MBID_2)

    await album_info.lookup(KEY)

    assert mb.paths()[:3] == ["/isrc/USIR10400001", "/isrc/USIR10400002", f"/release/{MBID_2}"]
    assert "/release/" not in mb.paths() and (await info_row(db)).source_id == MBID_2


async def test_a_rejected_release_is_skipped(db, mb, dz):
    await seed_album(db)
    async with db() as session:
        session.add(AlbumInfo(album_key=KEY, key_version="2", key_album_raw="Hot Fuss", key_artist_raw="The Killers",
                              status="not_found", rejected_json=json.dumps([f"musicbrainz:{MBID}"]), fetched_at=utc_now()))
        await session.commit()
    mb.routes["/release/"] = {"releases": [mb_hit(MBID, rg_id="rg-a"), mb_hit(MBID_2, rg_id="rg-b")]}
    mb.routes[f"/release/{MBID_2}"] = mb_release(MBID_2)

    await album_info.lookup(KEY)

    row = await info_row(db)
    assert f"/release/{MBID}" not in mb.paths() and row.source_id == MBID_2
    assert json.loads(row.rejected_json) == [f"musicbrainz:{MBID}"]


@pytest.mark.parametrize("status", [503, 429])
async def test_musicbrainz_unavailable_is_an_error_and_deezer_is_not_tried(db, mb, dz, status):
    await seed_album(db)
    mb.routes["/release/"] = MusicBrainzUnavailable(status)

    assert await album_info.lookup(KEY) == "error"

    row = await info_row(db)
    assert row.status == "error" and row.last_error == f"MusicBrainzUnavailable {status}"
    assert timedelta(minutes=59) < row.retry_at - utc_now() <= timedelta(hours=1)
    assert dz.calls == []


async def test_a_clean_musicbrainz_miss_falls_back_to_deezer(db, mb, dz):
    await seed_album(db, total_tracks=11)
    dz.routes["/search/album"] = {"data": [deezer_hit(1, nb_tracks=18, title="Hot Fuss (Deluxe)"), deezer_hit(),
                                           deezer_hit(302128, explicit=True)]}
    dz.routes["/album/302127"] = deezer_album()
    pages = {0: {"data": deezer_tracks(HOT_FUSS[:6], start=0),
                 "next": "https://api.deezer.com/album/302127/tracks?limit=100&index=6"},
             6: {"data": deezer_tracks(HOT_FUSS[6:], start=6)}}
    dz.routes["/album/302127/tracks"] = lambda params: pages[params["index"]]

    assert await album_info.lookup(KEY) == "found"

    assert dict(dz.calls)["/search/album"] == {"q": 'artist:"The Killers" album:"Hot Fuss"', "limit": 25}
    assert "/album/1" not in dz.paths() and "/album/302128" not in dz.paths()
    assert [p["index"] for path, p in dz.calls if path == "/album/302127/tracks"] == [0, 6]
    row = await info_row(db)
    assert (row.status, row.source, row.source_id, row.barcode, row.label) == (
        "found", "deezer", "302127", "602498617373", "Island")
    assert row.cover_url.startswith("https://cdn-images.dzcdn.net/") and row.media_format == "Digital"
    tracks = json.loads(row.tracks_json)
    assert len(tracks) == 11 and tracks[6] == {"disc": 1, "position": 1, "title": HOT_FUSS[6], "artist": "The Killers",
                                               "duration_ms": duration(6) // 1000 * 1000, "isrc": "USIR10400006",
                                               "source_track_id": "3006"}


async def test_deezer_unavailable_is_an_error(db, mb, dz):
    await seed_album(db)
    dz.routes["/search/album"] = DeezerUnavailable("error 4")

    assert await album_info.lookup(KEY) == "error"
    assert (await info_row(db)).last_error == "DeezerUnavailable error 4"


async def test_no_match_anywhere_is_not_found_for_a_week(db, mb, dz):
    await seed_album(db)

    assert await album_info.lookup(KEY) == "not_found"

    row = await info_row(db)
    assert row.status == "not_found" and timedelta(days=6, hours=23) < row.retry_at - utc_now() <= timedelta(days=7)


async def test_a_forced_refresh_replaces_the_match_even_at_lower_confidence(db, mb, dz):
    await seed_album(db, titles=HOT_FUSS[:10])
    await found_row(db, source_id=MBID)
    mb.routes["/release/"] = {"releases": [mb_hit(MBID_2, score=85)]}
    mb.routes[f"/release/{MBID_2}"] = mb_release(MBID_2)

    await album_info.lookup(KEY)

    row = await info_row(db)
    assert row.source_id == MBID_2 and row.confidence < 1.0


@pytest.mark.parametrize("failure, notice", [
    (None, "No other match found; keeping the info from MusicBrainz."),
    (MusicBrainzUnavailable(503), "Album info service unavailable; kept the current info.")])
async def test_a_failed_refresh_keeps_the_found_info(db, mb, dz, failure, notice):
    await seed_album(db)
    await found_row(db)
    if failure:
        mb.routes["/release/"] = failure

    assert await album_info.lookup(KEY) == "kept"

    row = await info_row(db)
    assert (row.status, row.source_id) == ("found", MBID)
    assert album_info.pop_notice(KEY) == notice


async def test_not_this_album_forgets_the_match_and_skips_it_next_time(db, mb, dz):
    await seed_album(db)
    await found_row(db)
    async with db() as session:
        assert await album_info.reject(session, KEY)
    row = await info_row(db)
    assert row.status == "not_found" and row.source_id is None
    assert json.loads(row.rejected_json) == [f"musicbrainz:{MBID}", "musicbrainz-rg:rg-hot"]
    mb.routes["/release/"] = {"releases": [mb_hit(MBID), mb_hit(MBID_2, rg_id="rg-other")]}
    mb.routes[f"/release/{MBID_2}"] = mb_release(MBID_2, rg_id="rg-other")

    await album_info.lookup(KEY)

    assert f"/release/{MBID}" not in mb.paths() and (await info_row(db)).source_id == MBID_2


async def test_not_this_album_during_a_lookup_is_not_undone_by_it(db, mb, dz, monkeypatch):
    await seed_album(db)
    await found_row(db)
    mb.routes["/release/"] = {"releases": [mb_hit()]}
    mb.routes[f"/release/{MBID}"] = mb_release()
    search = album_info.best_musicbrainz

    async def search_then_reject(local, rejected):
        pick = await search(local, rejected)
        async with db() as session:
            assert await album_info.reject(session, KEY)
        return pick

    monkeypatch.setattr(album_info, "best_musicbrainz", search_then_reject)

    assert await album_info.lookup(KEY, sources=("musicbrainz",)) == "superseded"

    row = await info_row(db)
    assert (row.status, row.source_id) == ("not_found", None)
    assert f"musicbrainz:{MBID}" in json.loads(row.rejected_json)


async def test_not_this_album_drops_the_rejected_releases_cached_cover(db):
    from services import artwork

    await seed_album(db)
    await found_row(db)
    cover = artwork.album_cover_path(KEY)
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"jpg")

    async with db() as session:
        assert await album_info.reject(session, KEY)

    assert not cover.exists()


async def test_a_collab_album_is_searched_by_its_first_credited_artist(db, mb, dz):
    await seed_album(db, titles=HOT_FUSS[:3], artist="Metro Boomin, Future", album_artist="Metro Boomin, Future")
    key = album_key("Hot Fuss", "Metro Boomin, Future", "Metro Boomin, Future").key

    await album_info.lookup(key)

    queries = [params["query"] for path, params in mb.calls if path == "/release/"]
    assert queries and all('artist:"Metro Boomin"' in q for q in queries)
    assert dict(dz.calls)["/search/album"]["q"].startswith('artist:"Metro Boomin"')


@pytest.mark.parametrize("url, pin", [
    (f"https://musicbrainz.org/release/{MBID}", ("musicbrainz", MBID)),
    (f"https://musicbrainz.org/release/{MBID.upper()}/cover-art", ("musicbrainz", MBID)),
    ("https://www.deezer.com/album/302127", ("deezer", "302127")),
    ("https://www.deezer.com/fr/album/302127?utm=x", ("deezer", "302127")),
    ("https://example.com/release/1", None), ("https://musicbrainz.org/artist/" + MBID, None), ("", None)])
def test_release_links_parse(url, pin):
    assert album_info.parse_release_url(url) == pin


async def test_a_pinned_musicbrainz_release_skips_search_and_gates(db, mb, dz):
    await seed_album(db, titles=HOT_FUSS[:3])
    mb.routes[f"/release/{MBID}"] = mb_release(title="Something Else", artist="Other")

    assert await album_info.lookup(KEY, pin=("musicbrainz", MBID)) == "found"

    row = await info_row(db)
    assert (row.pinned, row.confidence, row.title) == (True, 1.0, "Something Else")
    assert mb.paths() == [f"/release/{MBID}"]


async def test_a_pinned_release_is_refreshed_from_its_own_id(db, mb, dz):
    await seed_album(db)
    await found_row(db, pinned=True)
    mb.routes[f"/release/{MBID}"] = mb_release()

    await album_info.lookup(KEY)

    assert mb.paths() == [f"/release/{MBID}"] and (await info_row(db)).pinned


async def test_a_pinned_deezer_album_is_stored(db, mb, dz):
    await seed_album(db)
    dz.routes["/album/302127"] = deezer_album()
    dz.routes["/album/302127/tracks"] = {"data": deezer_tracks()}

    await album_info.lookup(KEY, pin=("deezer", "302127"))

    row = await info_row(db)
    assert (row.source, row.source_id, row.pinned, row.total_tracks) == ("deezer", "302127", True, 11)


async def test_the_cover_is_cached_only_for_an_album_without_sidecars(db, mb, dz, monkeypatch, tmp_path):
    from services import artwork
    from tests.test_tagger import _jpeg

    fetched = []

    async def fetch(url):
        fetched.append(url)
        return _jpeg(600, 600)

    monkeypatch.setattr(album_info, "_fetch_cover", fetch)
    await seed_album(db)
    mb.routes["/release/"] = {"releases": [mb_hit()]}
    mb.routes[f"/release/{MBID}"] = mb_release()

    await album_info.lookup(KEY)

    assert fetched == [f"https://coverartarchive.org/release/{MBID}/front-500"]
    assert artwork.album_cover_path(KEY).is_file()
    artwork.album_cover_path(KEY).unlink()


async def test_no_cover_is_fetched_when_a_song_has_a_sidecar(db, mb, dz, monkeypatch):
    import shutil

    from config import settings

    fetched = []

    async def fetch(url):
        fetched.append(url)

    monkeypatch.setattr(album_info, "_fetch_cover", fetch)
    folder = settings.library_dir / "sidecar-test" / "Hot Fuss"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "02 - Mr. Brightside.jpg").write_bytes(b"jpg")
    await seed_album(db, titles=HOT_FUSS[:1])
    await seed(db, track_number=2, duration_ms=duration(1), file_path=str(folder / "02 - Mr. Brightside.m4a"))
    await seed_album(db, titles=HOT_FUSS[2:], numbers=range(3, 12))
    mb.routes["/release/"] = {"releases": [mb_hit()]}
    mb.routes[f"/release/{MBID}"] = mb_release()

    try:
        await album_info.lookup(KEY)
    finally:
        shutil.rmtree(folder.parent)

    assert (await info_row(db)).status == "found" and fetched == []


async def test_no_session_is_open_during_network_calls(db, mb, dz):
    await seed_album(db)
    mb.routes["/release/"] = {"releases": [mb_hit()]}
    mb.routes[f"/release/{MBID_2}"] = None
    seen = []
    mb.on_call = dz.on_call = lambda path: seen.append(album_info._open_sessions)

    await album_info.lookup(KEY)

    assert seen and set(seen) == {0}


async def test_a_worst_case_musicbrainz_lookup_stays_within_budget(db, mb, dz):
    for i, title in enumerate(HOT_FUSS[:4], 1):
        await seed(db, title=title, track_number=i, duration_ms=duration(i - 1), isrc=f"USIR1040000{i}")
    mb.routes.update({f"/isrc/USIR1040000{i}": {"recordings": []} for i in range(1, 5)})
    mb.routes["/release/"] = {"releases": [mb_hit(m, rg_id=f"rg-{i % 2}", track_count=4) for i, m in
                                           enumerate([MBID, MBID_2, MBID_3, "44444444-4444-4444-4444-444444444444"])]}

    await album_info.lookup(KEY)

    assert len(mb.calls) <= 8
    assert sum(p.startswith("/isrc/") for p in mb.paths()) == 3 and mb.paths().count("/release/") == 1


async def test_concurrent_writes_for_one_album_never_conflict(db):
    await seed_album(db, titles=HOT_FUSS[:1])
    async with db() as session:
        album, rows = await local_albums.load(session, KEY)
    local = album_info.snapshot(album, rows)
    values = {"status": "not_found", "fetched_at": utc_now(), "attempts": 1}

    await asyncio.gather(*(album_info._upsert(local, values) for _ in range(5)))

    async with db() as session:
        assert len((await session.scalars(select(AlbumInfo))).all()) == 1


async def test_a_loose_group_is_never_looked_up(db, mb, dz):
    await seed(db, album=None)

    assert await album_info.lookup("loose") == "skipped"
    assert mb.calls == [] and dz.calls == []
