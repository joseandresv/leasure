from dataclasses import dataclass

import pytest

from services.album_merge import edition_fit, match_stats, merge, strip_suffixes, variant_clash


@dataclass
class Row:
    title: str
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    qobuz_isrc: str | None = None
    status: str = "done"
    id: int = 0


def rel(disc, position, title, duration_ms=200_000, isrc=None):
    return {"disc": disc, "position": position, "title": title, "artist": "A", "duration_ms": duration_ms,
            "isrc": isrc, "source_track_id": f"{disc}-{position}"}


def placed(rows):
    return [(r.release and (r.release["disc"], r.release["position"]), r.track and r.track.title, r.match, r.on_release)
            for r in rows]


def test_isrc_beats_a_title_match():
    song = Row("Intro", track_number=1, isrc="USAAA0000002")
    release = [rel(1, 1, "Intro", isrc="USAAA0000001"), rel(1, 2, "Intro (Reprise)", isrc="USAAA0000002")]

    rows = merge([song], release)

    assert placed(rows) == [((1, 1), None, None, True), ((1, 2), "Intro", "isrc", True)]


def test_a_title_match_needs_durations_within_ten_seconds():
    rows = merge([Row("Song", duration_ms=200_000)], [rel(1, 1, "Song", duration_ms=215_000)])

    assert rows[0].track is None and rows[1].on_release is False


@pytest.mark.parametrize("local", ["Welcome to New York (Taylor's Version)", "Song (Live)", "Love (Japanese ver.)"])
def test_a_variant_never_matches_the_plain_title(local):
    plain = {"Welcome to New York (Taylor's Version)": "Welcome to New York"}.get(local, local.split(" (")[0])
    rows = merge([Row(local, track_number=1, duration_ms=200_000)], [rel(1, 1, plain)])

    assert rows[0].track is None and rows[-1].on_release is False


def test_a_position_match_needs_a_similar_title_and_close_durations():
    near = merge([Row("Mr. Bright Side", track_number=2, duration_ms=222_000)],
                 [rel(1, 1, "Jenny"), rel(1, 2, "Mr. Brightside", duration_ms=224_000)])
    far = merge([Row("Mr. Bright Side", track_number=2, duration_ms=222_000)],
                [rel(1, 1, "Jenny"), rel(1, 2, "Mr. Brightside", duration_ms=230_000)])
    unknown = merge([Row("Mr. Bright Side", track_number=2)], [rel(1, 1, "Jenny"), rel(1, 2, "Mr. Brightside")])

    assert near[1].match == "position"
    assert far[1].track is None
    assert unknown[1].track is None


def test_a_position_match_with_a_different_song_is_refused():
    rows = merge([Row("Completely Other", track_number=1, duration_ms=200_000)], [rel(1, 1, "Jenny")])

    assert rows[0].track is None


def test_the_same_title_on_two_discs_goes_to_each_rows_own_disc():
    release = [rel(1, 1, "Intro"), rel(2, 1, "Intro")]
    rows = merge([Row("Intro", track_number=1, disc_number=2, id=2), Row("Intro", track_number=1, disc_number=1, id=1)],
                 release)

    assert [(r.release["disc"], r.track.id) for r in rows] == [(1, 1), (2, 2)]


def test_bonus_rows_follow_the_last_track_of_their_disc():
    release = [rel(1, 1, "A"), rel(1, 2, "B"), rel(2, 1, "C")]
    rows = merge([Row("A", 1), Row("Bonus", 9, 1), Row("C", 1, 2), Row("Disc 2 Bonus", 5, 2)], release)

    assert [(r.release and r.release["title"], r.track and r.track.title, r.on_release) for r in rows] == [
        ("A", "A", True), ("B", None, True), (None, "Bonus", False), ("C", "C", True), (None, "Disc 2 Bonus", False)]


def test_without_a_release_the_rows_keep_their_order():
    songs = [Row("B", 2), Row("A", 1)]

    assert [(r.track.title, r.on_release) for r in merge(songs, None)] == [("B", None), ("A", None)]


def test_overlap_ignores_position_matches_and_counts_position_fit():
    release = [rel(1, 1, "Jenny"), rel(1, 2, "Mr. Brightside", duration_ms=224_000), rel(1, 3, "Smile")]
    songs = [Row("Jenny", 1), Row("Mr. Bright Side", 2, duration_ms=222_000), Row("Smile", 4)]

    stats = match_stats(merge(songs, release), n_local=3)

    assert stats.matched == 2 and stats.overlap == pytest.approx(2 / 3)
    assert stats.position_fit == 0.5


@pytest.mark.parametrize("a, b, fit", [
    ("Hot Fuss", "hot fuss", 1.0), ("Hot Fuss (Deluxe Edition)", "Hot Fuss", 0.8), ("Thriller - EP", "Thriller", 0.8),
    ("A Night at the Opera (2011 Remaster)", "A Night at the Opera", 1.0), ("Greatest Hits II", "Greatest Hits", None),
    ("Greatest Hits Live", "Greatest Hits", None), ("1989 (Taylor's Version)", "1989", None),
    ("Live at Wembley", "Live", None), ("Love Songs", "Love", None), ("The Wall", "Wall", None),
    ("Album 2", "Album", None)])
def test_edition_fit(a, b, fit):
    assert edition_fit(a, b) == fit
    assert edition_fit(b, a) == fit


def test_strip_suffixes():
    assert strip_suffixes("Hot Fuss (Deluxe Edition) [Japan]") == "Hot Fuss"
    assert strip_suffixes("Thriller - EP") == "Thriller"
    assert strip_suffixes("(What's the Story) Morning Glory?") == "(What's the Story) Morning Glory?"


def test_variant_clash_ignores_words_in_both():
    assert variant_clash("Song (Live)", "Song")
    assert not variant_clash("Song (Live)", "Song - Live")
