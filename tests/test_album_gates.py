import pytest

from services.album_info import LocalSnapshot, LocalSong, Release, score
from services.album_merge import primary
from tests.album_fakes import HOT_FUSS, duration

TWELVE = [*HOT_FUSS, "Glamorous Indie Rock & Roll"]
EIGHTEEN = [*TWELVE, *(f"Bonus {i}" for i in range(6))]


def local(numbers, titles=None, T=(), name="Hot Fuss", artist="The Killers", total_tracks=None):
    titles = titles or [TWELVE[n - 1] if n else f"Song {i}" for i, n in enumerate(numbers)]
    songs = tuple(LocalSong(id=i, title=t, artist=artist, album_artist=artist, isrc=None, qobuz_isrc=None,
                            track_number=n, disc_number=1, duration_ms=duration((n or i + 1) - 1), status="done",
                            file_path=None) for i, (t, n) in enumerate(zip(titles, numbers, strict=True)))
    return LocalSnapshot(key="a-" + "0" * 20, name=name, artist=artist, total_tracks=total_tracks or (max(T) if T else None),
                         total_tracks_values=frozenset(T), songs=songs, artists=frozenset({primary(artist)}))


def release(titles, title="Hot Fuss", artist="The Killers", primary_type="Album", secondary=()):
    tracks = [{"disc": 1, "position": i + 1, "title": t, "artist": artist, "duration_ms": duration(i), "isrc": None,
               "source_track_id": str(i)} for i, t in enumerate(titles)]
    return Release(source="musicbrainz", source_id="x", title=title, artist=artist, tracks=tracks,
                   primary_type=primary_type, secondary_types=tuple(secondary))


def test_a_full_standard_album_scores_one():
    result = score(local(range(1, 11), T={12}), release(TWELVE), 100)

    assert result.confidence == pytest.approx(1.0) and result.accepted


def test_the_deluxe_edition_fails_the_known_track_count():
    assert score(local(range(1, 11), T={12}), release(EIGHTEEN), 100) is None


def test_without_a_known_count_the_deluxe_is_too_long_and_the_standard_fits():
    rows = local(range(1, 11))
    standard = release(TWELVE[:10])

    assert score(rows, release(EIGHTEEN), 100) is None
    assert score(rows, standard, 100).confidence == pytest.approx(1.0)


def test_seven_rows_numbered_up_to_twelve_fit_the_twelve_track_release():
    assert score(local([1, 2, 3, 5, 8, 10, 12]), release(TWELVE), 100).confidence == pytest.approx(1.0)


def test_a_sequel_album_title_is_rejected():
    rows = local([1, 2, 3], titles=["A", "B", "C"], name="Greatest Hits", artist="Queen")

    assert score(rows, release(["A", "B", "C"], title="Greatest Hits II", artist="Queen"), 100) is None


def test_a_single_is_not_an_album_with_a_longer_name():
    rows = local([1], titles=["Love"], name="Love", artist="X")

    assert score(rows, release(["Love", "Other"], title="Love Songs", artist="X"), 100) is None


def test_a_track_number_past_the_release_length_is_rejected():
    rows = local([3], titles=["Y"], name="X", artist="Z")

    assert score(rows, release(["Y"], title="X", artist="Z", primary_type="Single"), 100) is None
    assert score(rows, release(["A", "B", "Y", "C", "D"], title="X", artist="Z", primary_type="EP"), 100).accepted


def test_a_box_set_never_matches_a_two_song_album():
    assert score(local([1, 2]), release([*TWELVE, *(f"Extra {i}" for i in range(30))]), 100) is None


def test_a_live_album_of_the_same_name_is_rejected():
    assert score(local([1], titles=[HOT_FUSS[0]]), release(HOT_FUSS, secondary=("Live",)), 100) is None


def test_a_live_album_matches_when_the_library_calls_it_live():
    rows = local(range(1, 12), name="Hot Fuss Live", T={11})

    assert score(rows, release(HOT_FUSS, title="Hot Fuss Live", secondary=("Live",)), 100).accepted


def test_a_compilation_needs_the_exact_title():
    rows = local(range(1, 12), T={11}, name="Greatest Hits", artist="Queen")

    assert score(rows, release(HOT_FUSS, title="Greatest Hits", artist="Queen", secondary=("Compilation",)), 100)
    assert score(rows, release(HOT_FUSS, title="Greatest Hits (Deluxe)", artist="Queen",
                               secondary=("Compilation",)), 100) is None


def test_another_artist_is_rejected():
    assert score(local(range(1, 12), T={11}), release(HOT_FUSS, artist="Brandon Flowers"), 100) is None


def test_a_single_like_group_needs_a_single_that_starts_with_the_song():
    rows = local([1], titles=["Mr. Brightside"], name="Mr. Brightside")

    assert score(rows, release(["Mr. Brightside", "Who Let You Go?"], title="Mr. Brightside"), 100) is None
    assert score(rows, release(["Mr. Brightside", "Who Let You Go?"], title="Mr. Brightside",
                               primary_type="Single"), 100).accepted
    assert score(rows, release(["Who Let You Go?", "Mr. Brightside"], title="Mr. Brightside",
                               primary_type="Single"), 100) is None


def test_two_local_rows_must_both_match_in_place():
    moved = local([1, 3], titles=[TWELVE[0], TWELVE[1]], T={12})

    assert score(local([1, 2], T={12}), release(TWELVE), 100).accepted
    assert score(moved, release(TWELVE), 100) is None


def test_most_rows_must_match_on_a_bigger_album():
    rows = local([1, 2, 3, 4], titles=[TWELVE[0], TWELVE[1], "Unknown One", "Unknown Two"], T={12})

    assert score(rows, release(TWELVE), 100) is None


def test_weights_are_renormalised_when_no_row_has_a_number():
    rows = local([None] * 11, titles=HOT_FUSS, T={12})

    result = score(rows, release(TWELVE), 80)

    assert result.position_fit is None
    assert result.confidence == pytest.approx((0.35 + 0.25 + 0.10 + 0.10 * 0.8) / 0.80)
