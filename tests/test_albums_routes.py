import json
import shutil

import pytest
from fastapi.responses import HTMLResponse
from sqlalchemy import func, inspect, select

from config import settings
from models import AlbumInfo, Track, utc_now
from services import album_info, artwork, deezer, local_albums, musicbrainz
from services.local_albums import album_key
from tests.album_fakes import HOT_FUSS, MBID, FakeAPI, duration, mb_release, seed, seed_album

KEY = album_key("Hot Fuss", "The Killers", "The Killers").key
URL = f"/api/albums/{KEY}"
HTMX = {"HX-Request": "true"}


@pytest.fixture
def rendered(monkeypatch):
    """Every template the albums router renders, as (name, context); nothing is really rendered."""
    from routers import albums

    calls: list[tuple[str, dict]] = []

    def spy(*args, **kwargs):
        calls.append((kwargs["name"], kwargs["context"]))
        return HTMLResponse("", status_code=kwargs.get("status_code", 200))

    monkeypatch.setattr(albums.templates, "TemplateResponse", spy)
    return calls


@pytest.fixture
def library_folder():
    folder = settings.library_dir / "routes-test"
    folder.mkdir(parents=True, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def release_tracks(titles=HOT_FUSS, discs=None):
    discs = discs or [titles]
    tracks, i = [], 0
    for d, disc_titles in enumerate(discs, 1):
        for p, title in enumerate(disc_titles, 1):
            tracks.append({"disc": d, "position": p, "title": title, "artist": "The Killers",
                           "duration_ms": duration(i), "isrc": None, "source_track_id": f"rec-{i}"})
            i += 1
    return tracks


async def store_release(db, tracks=None, confidence=1.0, **fields):
    tracks = tracks if tracks is not None else release_tracks()
    async with db() as session:
        session.add(AlbumInfo(
            album_key=KEY, key_version=local_albums.KEY_VERSION, key_album_raw="Hot Fuss",
            key_artist_raw="The Killers", status="found", source="musicbrainz", source_id=MBID,
            release_group_id="rg-hot", confidence=confidence, title="Hot Fuss", artist="The Killers",
            release_date="2004-06-07", year=2004, label="Island", total_tracks=len(tracks),
            total_discs=max(t["disc"] for t in tracks), tracks_json=json.dumps(tracks), genres_json='["rock"]',
            fetched_at=utc_now(), **fields))
        await session.commit()


async def track_snapshot(db) -> dict[int, dict]:
    async with db() as session:
        rows = (await session.scalars(select(Track))).all()
        return {row.id: {c.key: getattr(row, c.key) for c in inspect(Track).mapper.column_attrs} for row in rows}


async def count_tracks(db, **where) -> int:
    async with db() as session:
        stmt = select(func.count()).select_from(Track)
        for name, value in where.items():
            stmt = stmt.where(getattr(Track, name) == value)
        return await session.scalar(stmt)


def context_of(rendered, name="partials/album_view.html") -> dict:
    return next(ctx for template, ctx in reversed(rendered) if template == name)


# --- the view ---


async def test_the_view_context_has_the_template_contract(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:10], year=2004, total_tracks=11, genre="rock")
    await store_release(db)

    resp = await client.get(f"{URL}/html")

    ctx = context_of(rendered)
    assert resp.status_code == 200
    assert set(ctx) == {"key", "provider", "album_id", "album", "tracks", "multi_disc", "info", "downloads_allowed",
                        "bulk_download_allowed", "urls", "upgrade_track_ids", "upgrade_possible"}
    assert set(ctx["album"]) == {
        "key", "kind", "name", "artist", "album_artist", "year", "release_date", "label", "catalog_number", "barcode",
        "media_format", "total_tracks", "total_discs", "total_duration_ms", "local_count", "missing_count",
        "elsewhere_count", "bonus_count", "image_url", "genres"}
    assert set(ctx["tracks"][0]) == {
        "name", "artist", "disc_number", "track_number", "release_position", "duration_ms", "isrc", "state",
        "download_status", "db", "track_id", "on_release", "match", "elsewhere", "download_url"}
    assert set(ctx["info"]) == {
        "status", "pending", "should_ensure", "can_enrich", "mismatch", "pinned", "artist_warning", "source",
        "source_label", "source_url", "confidence", "fetched_at", "retry_at", "release_title"}
    assert set(ctx["urls"]) == {"self", "ensure", "status", "refresh_info", "reject_info", "pin_info",
                                "download_missing", "cover"}
    assert (ctx["provider"], ctx["album_id"], ctx["key"]) == ("library", KEY, KEY)
    assert (ctx["album"]["local_count"], ctx["album"]["missing_count"], ctx["album"]["total_tracks"]) == (10, 1, 11)
    assert ctx["tracks"][10]["state"] == "missing"
    assert ctx["tracks"][10]["download_url"] == f"/api/albums/{KEY}/download/1/11"
    assert ctx["info"]["source_url"] == f"https://musicbrainz.org/release/{MBID}"
    assert ctx["bulk_download_allowed"] and ctx["downloads_allowed"]


async def test_the_view_reads_only_the_database(client, db, rendered, monkeypatch):
    monkeypatch.setattr(settings, "album_info_lookups", True)
    await seed_album(db, titles=HOT_FUSS[:3])
    before = await track_snapshot(db)

    await client.get(f"{URL}/html")

    ctx = context_of(rendered)
    assert ctx["info"]["status"] == "none" and ctx["info"]["should_ensure"]
    assert album_info._queue.qsize() == 0 and album_info.pending_for(KEY) is None
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(AlbumInfo)) == 0
    assert await track_snapshot(db) == before


async def test_a_view_that_starts_the_lookup_says_it_is_looking(client, db, rendered, monkeypatch):
    monkeypatch.setattr(settings, "album_info_lookups", True)
    await seed_album(db, titles=HOT_FUSS[:3])

    await client.get(f"{URL}/html")

    assert context_of(rendered)["info"]["pending"]


@pytest.mark.parametrize("titles, minutes_shown", [(HOT_FUSS[:4], False), (HOT_FUSS, True)], ids=["partial", "complete"])
async def test_without_album_info_the_length_is_shown_only_for_a_complete_album(client, db, rendered, titles,
                                                                                minutes_shown):
    await seed_album(db, titles=titles, total_tracks=11)

    await client.get(f"{URL}/html")

    assert (context_of(rendered)["album"]["total_duration_ms"] is not None) == minutes_shown


async def test_a_pending_song_shows_as_queued_like_the_download_answer(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:3])
    await seed(db, title=HOT_FUSS[3], track_number=4, status="pending")

    await client.get(f"{URL}/html")

    assert [t["download_status"] for t in context_of(rendered)["tracks"]] == ["done", "done", "done", "queued"]


async def test_without_album_info_the_view_lists_the_library_songs(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:3])

    await client.get(f"{URL}/html")

    ctx = context_of(rendered)
    assert [t["name"] for t in ctx["tracks"]] == HOT_FUSS[:3]
    assert {t["state"] for t in ctx["tracks"]} == {"local"} and not ctx["downloads_allowed"]
    assert ctx["info"]["status"] == "disabled"


@pytest.mark.parametrize("key", ["zzz", "a-123", "a-" + "g" * 20])
async def test_a_bad_key_is_not_found(client, db, key):
    assert (await client.get(f"/api/albums/{key}/html")).status_code == 404


async def test_an_album_no_longer_in_the_library_says_so(client, db):
    resp = await client.get(f"{URL}/html")

    assert resp.status_code == 200 and "no longer in your library" in resp.text
    assert (await client.get(URL)).status_code == 404


async def test_loose_songs_open_as_their_own_view(client, db, rendered):
    await seed(db, title="Stray", album=None)

    await client.get("/api/albums/loose/html")

    ctx = context_of(rendered)
    assert ctx["album"]["kind"] == "loose" and ctx["album"]["name"] == "Loose songs"
    assert ctx["info"]["status"] == "never" and not ctx["info"]["can_enrich"]


async def test_a_song_on_another_album_is_shown_as_elsewhere(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:10], total_tracks=11)
    other = await seed(db, title=HOT_FUSS[10], album="Live from the Royal Albert Hall", duration_ms=duration(10))
    await store_release(db)

    await client.get(f"{URL}/html")

    last = context_of(rendered)["tracks"][10]
    assert last["state"] == "elsewhere" and last["download_url"] is None
    assert last["elsewhere"]["key"] == local_albums.key_of("Live from the Royal Albert Hall", "The Killers",
                                                           "The Killers")
    assert other != last["track_id"]


@pytest.mark.parametrize("suffix", [" - LP Version", " (Explicit Version)", " - Original Version"])
async def test_a_song_with_a_release_format_suffix_is_never_offered_again(client, db, rendered, suffix):
    titles = [*HOT_FUSS[:9], HOT_FUSS[9] + suffix, HOT_FUSS[10]]
    await seed_album(db, titles=titles)
    await store_release(db)

    await client.get(f"{URL}/html")

    ctx = context_of(rendered)
    tenth = ctx["tracks"][9]
    assert tenth["state"] == "local" and tenth["download_url"] is None and tenth["on_release"]
    assert ctx["album"]["missing_count"] == 0 and not ctx["bulk_download_allowed"]


async def test_bonus_songs_are_not_counted_against_the_release_total(client, db, rendered):
    await seed_album(db, titles=[*HOT_FUSS[:10], "Glamorous Indie Rock & Roll"], numbers=[*range(1, 11), 12])
    await store_release(db, tracks=release_tracks(HOT_FUSS[:10]))

    await client.get(f"{URL}/html")

    album = context_of(rendered)["album"]
    assert (album["local_count"], album["total_tracks"], album["bonus_count"]) == (10, 10, 1)


async def test_the_loose_group_claims_no_year_or_track_count(client, db, rendered):
    await seed(db, title="Stray", album=None, year=2019, total_tracks=1)
    await seed(db, title="Other", album=None, year=2019, total_tracks=1)

    await client.get("/api/albums/loose/html")

    album = context_of(rendered)["album"]
    assert (album["year"], album["total_tracks"], album["local_count"]) == (None, None, 2)


async def test_the_json_view_has_album_tracks_and_info(client, db):
    await seed_album(db, titles=HOT_FUSS[:2])

    body = (await client.get(URL)).json()

    assert set(body) == {"album", "tracks", "info"} and body["album"]["local_count"] == 2


# --- the grid ---


async def test_the_album_list_filters_and_sorts(client, db):
    await seed(db, album="Hot Fuss", year=2004)
    await seed(db, title="When You Were Young", album="Sam's Town", year=2006)
    await seed(db, title="Stray", album=None)

    newest = (await client.get("/api/albums", params={"sort": "year"})).json()
    searched = (await client.get("/api/albums", params={"q": "sam"})).json()

    assert [a["name"] for a in newest["albums"]] == ["Sam's Town", "Hot Fuss", "Loose songs"]
    assert newest["total"] == 3 and [a["name"] for a in searched["albums"]] == ["Sam's Town"]


async def test_the_album_grid_context(client, db, rendered):
    track_id = await seed(db, file_path="/x/a.m4a", quality="flac_16_44", is_lossless=True)
    await store_release(db, tracks=release_tracks(["Mr. Brightside"]))

    await client.get("/api/albums/html", params={"limit": 1})

    ctx = context_of(rendered, "partials/library_albums.html")
    (card,) = ctx["albums"]
    assert card["cover_url"] == f"/api/albums/{KEY}/cover?t={track_id}"
    assert card["quality_mix"] == [{"label": "FLAC", "count": 1}] and card["all_lossless"]
    assert card["info_source"] == "musicbrainz"
    assert (ctx["total"], ctx["next_offset"], ctx["sort"]) == (1, None, "artist")


# --- the cover ---


async def test_the_hinted_songs_sidecar_is_served_without_a_scan(client, db, library_folder, monkeypatch):
    audio = library_folder / "01 - Mr. Brightside.m4a"
    audio.with_suffix(".jpg").write_bytes(b"hinted")
    track_id = await seed(db, file_path=str(audio))

    async def no_scan(*args):
        raise AssertionError("scanned")

    monkeypatch.setattr(local_albums, "load", no_scan)

    resp = await client.get(f"{URL}/cover", params={"t": track_id})

    assert resp.status_code == 200 and resp.content == b"hinted"


async def test_the_cover_falls_back_from_sidecar_to_cache_to_proxy(client, db, library_folder):
    audio = library_folder / "02 - Smile.m4a"
    await seed(db, title="Smile", file_path=str(audio), artwork_url="https://i.scdn.co/image/abc")
    cached = artwork.album_cover_path(KEY)

    assert (await client.get(f"{URL}/cover")).headers["location"] == (
        "/api/art/proxy?url=https%3A%2F%2Fi.scdn.co%2Fimage%2Fabc")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"cached")
    try:
        assert (await client.get(f"{URL}/cover")).content == b"cached"
        audio.with_suffix(".jpg").write_bytes(b"sidecar")
        assert (await client.get(f"{URL}/cover")).content == b"sidecar"
    finally:
        cached.unlink()


async def test_no_cover_at_all_is_404(client, db):
    await seed(db, artwork_url="https://evil.example/x.jpg")

    assert (await client.get(f"{URL}/cover")).status_code == 404


# --- downloads ---


async def test_a_missing_song_becomes_a_row_with_the_albums_names_and_cover(client, db, library_folder, rendered):
    ids = await seed_album(db, titles=HOT_FUSS[:10], artist="THE KILLERS", year=2004, total_tracks=11,
                           total_discs=1, genre="alternative rock")
    async with db() as session:
        sibling = await session.get(Track, ids[3])
        sibling.file_path = str(library_folder / "04 - Somebody Told Me.m4a")
        await session.commit()
    (library_folder / "04 - Somebody Told Me.jpg").write_bytes(b"jpg")
    await store_release(db)

    resp = await client.post(f"{URL}/download/1/11")

    assert resp.json()["status"] == "queued"
    async with db() as session:
        new = await session.get(Track, resp.json()["track_id"])
    assert (new.title, new.artist, new.album, new.album_artist) == (
        "Everything Will Be Alright", "THE KILLERS", "Hot Fuss", "The Killers")
    assert (new.track_number, new.disc_number, new.total_tracks, new.total_discs) == (11, 1, 11, 1)
    assert (new.year, new.genre, new.source, new.quality) == (2004, "alternative rock", "musicbrainz", "native")
    assert new.artwork_url == f"/api/library/art/{ids[3]}" and new.duration_ms == duration(10)
    assert new.spotify_uri is None and new.youtube_id is None


async def test_a_two_disc_release_does_not_split_a_single_disc_album(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    await store_release(db, tracks=release_tracks(discs=[HOT_FUSS[:6], HOT_FUSS[6:]]))

    resp = await client.post(f"{URL}/download/2/5")

    async with db() as session:
        new = await session.get(Track, resp.json()["track_id"])
    assert (new.disc_number, new.track_number, new.total_discs, new.total_tracks) == (1, 11, 1, 11)


async def test_a_two_disc_album_keeps_the_release_numbering(client, db):
    for i, title in enumerate(HOT_FUSS[:10]):
        disc, number = (1, i + 1) if i < 6 else (2, i - 5)
        await seed(db, title=title, disc_number=disc, track_number=number, total_discs=2, duration_ms=duration(i))
    await store_release(db, tracks=release_tracks(discs=[HOT_FUSS[:6], HOT_FUSS[6:]]))

    resp = await client.post(f"{URL}/download/2/5")

    async with db() as session:
        new = await session.get(Track, resp.json()["track_id"])
    assert (new.disc_number, new.track_number, new.total_discs, new.total_tracks) == (2, 5, 2, 5)


async def test_a_featured_credit_keeps_the_release_track_artist(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    tracks = release_tracks()
    tracks[10]["artist"] = "The Killers, Toni Halliday"
    await store_release(db, tracks=tracks)

    resp = await client.post(f"{URL}/download/1/11")

    async with db() as session:
        assert (await session.get(Track, resp.json()["track_id"])).artist == "The Killers, Toni Halliday"


async def test_a_double_click_queues_once(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    await store_release(db)

    first = await client.post(f"{URL}/download/1/11", headers=HTMX)
    second = await client.post(f"{URL}/download/1/11")

    assert first.status_code == 200 and second.json()["status"] == "already_queued"
    assert await count_tracks(db, title=HOT_FUSS[10]) == 1


@pytest.mark.parametrize("album", ["Hot Fuss (Live)", None], ids=["other_album", "loose"])
async def test_a_song_the_library_has_elsewhere_is_never_downloaded_again(client, db, album):
    await seed_album(db, titles=HOT_FUSS[:10])
    await seed(db, title=HOT_FUSS[10], album=album, duration_ms=duration(10))
    await store_release(db)

    resp = await client.post(f"{URL}/download/1/11")

    assert resp.json()["status"] == "already_downloaded"
    assert await count_tracks(db, title=HOT_FUSS[10]) == 1


async def test_a_failed_song_of_another_album_is_left_alone(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    other = await seed(db, title=HOT_FUSS[10], album="Sam's Town", status="error", duration_ms=duration(10))
    await store_release(db)
    before = await track_snapshot(db)

    resp = await client.post(f"{URL}/download/1/11")

    assert resp.json()["status"] == "already_downloaded"
    assert (await track_snapshot(db))[other] == before[other]


async def test_a_failed_song_of_this_album_is_retried_as_it_is(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    failed = await seed(db, title=HOT_FUSS[10], track_number=11, duration_ms=duration(10), status="error",
                        error_message="boom", source="spotify", quality="mp3_320", spotify_uri="spotify:track:x")
    await store_release(db)
    before = (await track_snapshot(db))[failed]

    resp = await client.post(f"{URL}/download/1/11", data={"format": "native"})

    after = (await track_snapshot(db))[failed]
    assert resp.json() == {"status": "queued", "track_id": failed}
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"status", "error_message", "quality"}
    assert after["status"] == "pending" and after["source"] == "spotify"


async def test_downloads_are_refused_without_confirmed_album_info(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])

    resp = await client.post(f"{URL}/download/1/11")

    assert resp.status_code == 409


async def test_a_refused_htmx_download_swaps_an_error_badge_into_the_row(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:10])

    resp = await client.post(f"{URL}/download/1/11", headers=HTMX)

    assert resp.status_code == 200
    assert context_of(rendered, "partials/download_badge.html")["status"] == "error"


async def test_a_bad_format_in_download_missing_rerenders_the_view_for_htmx(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:8], total_tracks=11)
    await store_release(db)

    resp = await client.post(f"{URL}/download-missing", data={"format": "flac"}, headers=HTMX)

    assert resp.status_code == 200
    assert context_of(rendered)["notice"]
    assert await count_tracks(db) == 8


async def test_a_position_off_the_release_is_404(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    await store_release(db)

    assert (await client.post(f"{URL}/download/1/40")).status_code == 404


async def test_a_bad_format_is_400(client, db):
    await seed_album(db, titles=HOT_FUSS[:10])
    await store_release(db)

    assert (await client.post(f"{URL}/download/1/11", data={"format": "flac"})).status_code == 400


async def test_download_missing_queues_every_missing_song(client, db):
    await seed_album(db, titles=HOT_FUSS[:8], total_tracks=11)
    await store_release(db)

    resp = await client.post(f"{URL}/download-missing")

    assert resp.json()["queued"] == 3
    assert await count_tracks(db) == 11


@pytest.mark.parametrize("confidence, local_titles", [(0.85, HOT_FUSS[:8]), (1.0, HOT_FUSS[:3] + ["Nope 1", "Nope 2"])],
                         ids=["low_confidence", "mismatch"])
async def test_download_missing_needs_a_confident_current_match(client, db, rendered, confidence, local_titles):
    await seed_album(db, titles=local_titles, total_tracks=11)
    await store_release(db, confidence=confidence)

    plain = await client.post(f"{URL}/download-missing")
    htmx = await client.post(f"{URL}/download-missing", headers=HTMX)

    assert plain.status_code == 409 and htmx.status_code == 409
    assert "confident" in context_of(rendered)["notice"]
    assert await count_tracks(db) == len(local_titles)


# --- info actions ---


async def test_a_bad_release_link_is_400(client, db):
    await seed_album(db, titles=HOT_FUSS[:2])

    resp = await client.post(f"{URL}/info/pin", data={"url": "https://example.com"})

    assert resp.status_code == 400


async def test_a_bad_release_link_from_the_view_lands_in_the_form_error_slot(client, db):
    await seed_album(db, titles=HOT_FUSS[:2])

    resp = await client.post(f"{URL}/info/pin", data={"url": "https://example.com"}, headers=HTMX)

    assert resp.status_code == 200 and resp.text == "Paste a MusicBrainz release link or a Deezer album link."
    assert resp.headers["HX-Retarget"] == f"#album-view-{KEY} .album-pin-error"
    assert resp.headers["HX-Reswap"] == "innerHTML"


async def test_refresh_with_lookups_off_says_so(client, db, rendered):
    await seed_album(db, titles=HOT_FUSS[:2])

    await client.post(f"{URL}/info/refresh")

    assert context_of(rendered)["notice"] == "Album info lookups are turned off."


async def test_no_track_row_changes_through_enrichment_and_every_action(client, db, rendered, monkeypatch):
    """R5: album info never writes existing Track rows; only an explicit download adds new ones."""
    monkeypatch.setattr(settings, "album_info_lookups", True)
    await seed_album(db, titles=HOT_FUSS[:9], total_tracks=11, year=2004, genre="rock")
    await seed(db, title=HOT_FUSS[9], track_number=10, duration_ms=duration(9), status="pending", quality="native")
    await seed(db, title="Somebody Else", track_number=12, status="error", error_message="x")
    await seed(db, title="Other Song", album="Sam's Town", status="done")
    api = FakeAPI({"/release/": {"releases": [{
        "id": MBID, "score": 100, "title": "Hot Fuss", "track-count": 11, "status": "Official",
        "artist-credit": [{"name": "The Killers"}], "release-group": {"id": "rg-hot", "primary-type": "Album"},
        "media": [{"format": "Digital Media", "track-count": 11}]}]}, f"/release/{MBID}": mb_release()})
    monkeypatch.setattr(musicbrainz, "get_checked", api)
    monkeypatch.setattr(deezer, "get_checked", FakeAPI({"/search/album": {"data": []}}))

    async def no_cover(url):
        return None

    monkeypatch.setattr(album_info, "_fetch_cover", no_cover)
    before = await track_snapshot(db)

    assert await album_info.lookup(KEY) == "found"
    await client.get(f"{URL}/html")
    await client.post(f"{URL}/info/refresh")
    await album_info._queue.join()
    await client.post(f"{URL}/info/reject")
    await album_info._queue.join()
    await client.post(f"{URL}/info/pin", data={"url": f"https://musicbrainz.org/release/{MBID}"})
    await album_info._queue.join()
    await client.post(f"{URL}/download/1/11")
    await client.post(f"{URL}/download-missing")

    after = await track_snapshot(db)
    assert {k: v for k, v in after.items() if k in before} == before
    assert len(after) > len(before)
