import pytest

from services import qobuz_engine
from services.qobuz_engine import QobuzSkip, TrackSnapshot, resolve_matches
from tests.qobuz_fakes import FakeClient, qtrack


def row(title="So What", artist="Miles Davis", album="Kind of Blue", duration_ms=200_000, isrc=None,
        qobuz_id=None, method=None):
    return TrackSnapshot(id=1, title=title, artist=artist, album=album, album_artist=None, duration_ms=duration_ms,
                         isrc=isrc, qobuz_id=qobuz_id, qobuz_match_method=method, artwork_url=None,
                         file_path=None, quality="native")


def matched(wanted, *candidates, strict=False):
    try:
        return resolve_matches(FakeClient(search=list(candidates)), wanted, strict=strict)
    except QobuzSkip as skip:
        assert skip.reason == "no_match"
        return []


@pytest.mark.parametrize("wanted_title, offered", [
    ("Intro", qtrack(title="Outro")),
    ("Part 1", qtrack(title="Part 2")),
    ("Love Story", qtrack(title="Love Story", version="Taylor's Version")),
    ("Yesterday", qtrack(title="Yesterday", version="Mono")),
    ("So What", qtrack(title="So What", version="Live at Newport")),
])
def test_a_different_recording_is_rejected(wanted_title, offered):
    assert matched(row(title=wanted_title), offered) == []


def test_another_artist_is_rejected():
    assert matched(row(artist="Bill Evans"), qtrack()) == []


@pytest.mark.parametrize("wanted_title, offered", [
    ("Hey Jude", qtrack(title="Hey Jude", version="Remastered 2015")),
    ("Hey Jude - Remastered 2015", qtrack(title="Hey Jude")),
    ("So What", qtrack(title="So What", version="2009 Remaster")),
])
def test_benign_versions_still_match(wanted_title, offered):
    performer = "The Beatles" if "Jude" in wanted_title else "Miles Davis"
    offered = qtrack(**{**offered.__dict__, "performer": performer, "album_artist": performer})

    assert [m.track.id for m in matched(row(title=wanted_title, artist=performer), offered)] == ["q1"]


def test_an_isrc_hit_wins_with_the_isrc_method():
    client = FakeClient(search=[], by_isrc=[qtrack(id="isrc-hit")])

    (match,) = resolve_matches(client, row(isrc="USSM15900113"), strict=False)

    assert (match.track.id, match.method, match.score) == ("isrc-hit", "isrc", 6.0)


def test_a_track_more_than_10_seconds_off_is_rejected():
    assert matched(row(duration_ms=215_000), qtrack(duration_s=200)) == []


def test_strict_mode_rejects_a_text_match_from_another_album():
    other_album = qtrack(album_title="Greatest Jazz Hits Compilation")

    assert matched(row(), other_album) != []
    assert matched(row(), other_album, strict=True) == []


def test_a_stored_search_match_is_gated_again():
    wrong = qtrack(id="stored", title="So What", version="Live")
    client = FakeClient(search=[], by_id={"stored": wrong})

    with pytest.raises(QobuzSkip):
        resolve_matches(client, row(qobuz_id="stored", method="search"), strict=False)


def test_a_stored_id_match_is_fetched_by_id():
    client = FakeClient(search=[], by_id={"q9": qtrack(id="q9")})

    (match,) = resolve_matches(client, row(qobuz_id="q9", method="id"), strict=True)

    assert (match.track.id, match.method) == ("q9", "id")


def test_at_most_three_candidates_best_first():
    offered = [qtrack(id=f"q{i}", album_title="Kind of Blue" if i == 3 else "Other") for i in range(5)]

    matches = matched(row(), *offered)

    assert len(matches) == qobuz_engine._MAX_CANDIDATES and matches[0].track.id == "q3"


def test_no_match_names_the_track():
    with pytest.raises(QobuzSkip, match="'Miles Davis - So What'"):
        resolve_matches(FakeClient(search=[]), row(), strict=False)
