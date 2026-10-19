"""Pure album matching: the shared name/artist comparisons and the merge of a group's library
rows with a release tracklist. No I/O; nothing here builds a stored key."""

import re
from dataclasses import dataclass
from typing import Any

from services.card_import import similarity
from services.library_match import _edition_free
from services.qobuz_client import normalise_isrc
from services.qobuz_engine import _VARIANT_WORDS as _QOBUZ_VARIANT_WORDS

EDITION_WORDS = frozenset({"deluxe", "expanded", "edition", "anniversary", "bonus", "special", "version", "remaster",
                           "remastered", "reissue", "collector", "collectors", "super", "limited", "tracks", "track",
                           "ep", "single", "the"})
_NUMBER_WORD = re.compile(r"\d+(?:st|nd|rd|th)?")
_TRAILING_PART = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\]|\s[-–—]\s.*)\s*$")
VARIANT_WORDS = tuple(dict.fromkeys((*_QOBUZ_VARIANT_WORDS, "japanese ver", "korean ver", "english ver",
                                     "chinese ver", "inst", "version")))
TITLE_DURATION_S = 10
POSITION_DURATION_S = 5
POSITION_SIMILARITY = 0.8


def ef(text: str | None) -> str:
    """Edition-free comparable form; never empty for a non-blank title ("÷" stays "÷")."""
    return _edition_free(text) or " ".join((text or "").casefold().split())


def primary(artist: str | None) -> str:
    return ef((artist or "").split(",")[0])


def _has_words(text: str, words: str) -> bool:
    return f" {words} " in f" {text} "


def edition_fit(a: str | None, b: str | None) -> float | None:
    """1.0 for the same album name, 0.8 when one only adds edition words ("Deluxe Edition"), None otherwise."""
    x, y = ef(a).split(), ef(b).split()
    if not x or not y:
        return None
    if x == y:
        return 1.0
    short, long = sorted((x, y), key=len)
    for start in range(len(long) - len(short) + 1):
        if long[start:start + len(short)] != short:
            continue
        extra = long[:start] + long[start + len(short):]
        if (all(w in EDITION_WORDS or _NUMBER_WORD.fullmatch(w) for w in extra)
                and any(w in EDITION_WORDS and w != "the" for w in extra)):
            return 0.8
    return None


def strip_suffixes(name: str) -> str:
    """ "Hot Fuss (Deluxe Edition) [Japan]" -> "Hot Fuss"; "Thriller - EP" -> "Thriller"."""
    current = name.strip()
    while (stripped := _TRAILING_PART.sub("", current).strip()) and stripped != current:
        current = stripped
    return current


def artist_ok(credit: str | None, local_artists: set[str] | frozenset[str]) -> bool:
    """The credit's first artist is one of the local spellings, or one contains the other as whole words."""
    wanted = primary(credit)
    if not wanted:
        return False
    for have in local_artists:
        if have == wanted:
            return True
        if len(have) >= 3 and len(wanted) >= 3 and (_has_words(have, wanted) or _has_words(wanted, have)):
            return True
    return False


def variant_clash(a: str | None, b: str | None) -> bool:
    x, y = ef(a), ef(b)
    return any(_has_words(x, w) != _has_words(y, w) for w in VARIANT_WORDS)


@dataclass
class MergedRow:
    track: Any | None                 # a library row (Track or a snapshot with the same attributes)
    release: dict | None              # a ReleaseTrack dict
    match: str | None = None          # isrc | title | position
    on_release: bool | None = None


def _position(track) -> tuple[int, int] | None:
    return (track.disc_number or 1, track.track_number) if track.track_number else None


def _release_position(item: dict) -> tuple[int, int]:
    return item["disc"], item["position"]


def _duration_gap_s(track, item: dict) -> float | None:
    if not track.duration_ms or not item.get("duration_ms"):
        return None
    return abs(track.duration_ms - item["duration_ms"]) / 1000


def _claim_by_isrc(track, free: list[dict]) -> dict | None:
    isrc = normalise_isrc(track.isrc) or normalise_isrc(getattr(track, "qobuz_isrc", None))
    hits = [item for item in free if isrc and normalise_isrc(item.get("isrc")) == isrc]
    return next((item for item in hits if _release_position(item) == _position(track)), hits[0] if hits else None)


def _claim_by_title(track, free: list[dict]) -> dict | None:
    title = ef(track.title)
    hits = []
    for item in free:
        gap = _duration_gap_s(track, item)
        if ef(item["title"]) == title and not variant_clash(track.title, item["title"]) \
                and (gap is None or gap <= TITLE_DURATION_S):
            hits.append((_release_position(item) != _position(track), gap if gap is not None else 1e9, item))
    return min(hits, key=lambda hit: hit[:2])[2] if hits else None


def _claim_by_position(track, free: list[dict]) -> dict | None:
    position = _position(track)
    item = next((item for item in free if _release_position(item) == position), None) if position else None
    if item is None:
        return None
    gap = _duration_gap_s(track, item)
    if gap is None or gap > POSITION_DURATION_S or variant_clash(track.title, item["title"]):
        return None
    return item if similarity(ef(track.title), ef(item["title"])) >= POSITION_SIMILARITY else None


def merge(local_rows: list, release: list[dict] | None) -> list[MergedRow]:
    """The release tracklist with each library row placed on its track; rows the release lacks
    follow the last track of their disc as bonus rows. Without a release: the rows in local order."""
    if release is None:
        return [MergedRow(track=row, release=None) for row in local_rows]
    ordered = sorted(release, key=_release_position)
    claimed: dict[int, tuple[Any, str]] = {}
    pending = sorted(local_rows, key=lambda row: row.status != "done")
    for match, claim in (("isrc", _claim_by_isrc), ("title", _claim_by_title), ("position", _claim_by_position)):
        for row in list(pending):
            free = [item for i, item in enumerate(ordered) if i not in claimed]
            if (item := claim(row, free)) is not None:
                claimed[next(i for i, it in enumerate(ordered) if it is item)] = (row, match)
                pending.remove(row)
    rows = [MergedRow(track=claimed[i][0] if i in claimed else None, release=item,
                      match=claimed[i][1] if i in claimed else None, on_release=True)
            for i, item in enumerate(ordered)]
    for bonus in sorted(pending, key=lambda row: (row.disc_number or 1, row.track_number or 9999, row.title.casefold())):
        disc = bonus.disc_number or 1
        after = max((i for i, row in enumerate(rows) if (row.release or {}).get("disc") == disc
                     or (row.release is None and (row.track.disc_number or 1) == disc)), default=len(rows) - 1)
        rows.insert(after + 1, MergedRow(track=bonus, release=None, on_release=False))
    return rows


@dataclass(frozen=True)
class MatchStats:
    overlap: float
    position_fit: float | None
    matched: int


def match_stats(rows: list[MergedRow], n_local: int) -> MatchStats:
    """Overlap and position fit over the done rows the ISRC and title passes matched (position matches only place rows)."""
    matched = [row for row in rows if row.match in ("isrc", "title") and row.track.status == "done"]
    numbered = [row for row in matched if row.track.track_number]
    position_fit = (sum(_position(row.track) == _release_position(row.release) for row in numbered) / len(numbered)
                    if numbered else None)
    return MatchStats(overlap=len(matched) / n_local if n_local else 0.0, position_fit=position_fit,
                      matched=len(matched))
