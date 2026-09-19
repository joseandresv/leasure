import pytest

from scripts import retag


@pytest.fixture
def tagged(monkeypatch):
    """apply_full_metadata records (track_id, path) instead of touching a file."""
    calls = []

    async def fake_apply(path, track_id):
        calls.append((track_id, path))

    monkeypatch.setattr(retag, "apply_full_metadata", fake_apply)
    return calls


async def _seed(db, tmp_path, rows):
    from models import Track

    created = []
    async with db() as session:
        for index, row in enumerate(rows, 1):
            path = tmp_path / f"track{index}.mp3"
            if row.pop("on_disk", True):
                path.write_bytes(b"audio")
            track = Track(title=f"Song {index}", artist="Earth, Wind & Fire", status="done",
                          file_path=str(path), **row)
            session.add(track)
            created.append(track)
        await session.commit()
    return created


@pytest.mark.asyncio
async def test_dry_run_tags_nothing(db, tmp_path, tagged):
    await _seed(db, tmp_path, [{"genre": None}, {"genre": "soul"}])

    counts = await retag.retag(dry_run=True, only_missing=False, ids=None)

    assert tagged == []
    assert (counts["scanned"], counts["tagged"]) == (2, 0)


@pytest.mark.asyncio
async def test_retag_tags_every_finished_track(db, tmp_path, tagged):
    rows = await _seed(db, tmp_path, [{"genre": "soul", "album_artist": "EWF"}, {"genre": "funk"}])

    counts = await retag.retag(dry_run=False, only_missing=False, ids=None)

    assert [track_id for track_id, _ in tagged] == [rows[0].id, rows[1].id]
    assert counts["tagged"] == 2


@pytest.mark.asyncio
async def test_only_missing_skips_rows_that_have_album_artist_and_genre(db, tmp_path, tagged):
    rows = await _seed(db, tmp_path, [{"genre": "soul", "album_artist": "EWF"},
                                      {"genre": "funk", "album_artist": None},
                                      {"genre": None, "album_artist": "EWF"}])

    await retag.retag(dry_run=False, only_missing=True, ids=None)

    assert [track_id for track_id, _ in tagged] == [rows[1].id, rows[2].id]


@pytest.mark.asyncio
async def test_a_track_whose_file_is_gone_is_counted_not_tagged(db, tmp_path, tagged):
    rows = await _seed(db, tmp_path, [{"on_disk": False}, {"genre": "funk"}])

    counts = await retag.retag(dry_run=False, only_missing=False, ids=None)

    assert [track_id for track_id, _ in tagged] == [rows[1].id]
    assert (counts["no_file"], counts["tagged"]) == (1, 1)


@pytest.mark.asyncio
async def test_ids_limit_the_run_to_those_tracks(db, tmp_path, tagged):
    rows = await _seed(db, tmp_path, [{"genre": "soul"}, {"genre": "funk"}, {"genre": "disco"}])

    counts = await retag.retag(dry_run=False, only_missing=False, ids=[rows[1].id])

    assert [track_id for track_id, _ in tagged] == [rows[1].id]
    assert counts["scanned"] == 1

