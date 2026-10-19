"""Which library row a provider track already is, under any key it carries, so no second copy is offered."""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Track
from services.card_import import normalize_title
from services.qobuz_client import normalise_isrc
from services.qobuz_engine import BENIGN_VERSION


@dataclass(frozen=True)
class TrackKeys:
    title: str
    artist: str
    album: str = ""
    album_aliases: tuple[str, ...] = ()
    youtube_id: str | None = None
    spotify_uri: str | None = None
    isrc: str | None = None


def _primary_artist(artist: str | None) -> str:
    return normalize_title((artist or "").split(",")[0])


def _edition_free(text: str | None) -> str:
    """Spotify's "X - Remastered 2011" and YouTube's "X (2011 Remaster)" compare equal."""
    return normalize_title(BENIGN_VERSION.sub(" ", (text or "").casefold())) or normalize_title(text)


def _song_key(title: str | None, artist: str | None) -> tuple[str, str]:
    return _edition_free(title), _primary_artist(artist)


def _row_song_keys(title: str | None, artist: str | None, album_artist: str | None) -> set[tuple[str, str]]:
    return {_song_key(title, artist), _song_key(title, album_artist)} if album_artist else {_song_key(title, artist)}


def _same_album(wanted: str, have: str | None) -> bool:
    """Deluxe and remaster editions count: one normalised name contains the other."""
    a, b = _edition_free(wanted), _edition_free(have)
    return bool(a and b) and (a in b or b in a)


def _isrc_conflict(wanted: str | None, row: Track) -> bool:
    have = normalise_isrc(row.isrc)
    return bool(wanted and have) and wanted != have


def _better(current: Track | None, candidate: Track) -> bool:
    return current is None or (current.status != "done" and candidate.status == "done")


class LibraryIndex:
    def __init__(self, rows: list[Track]):
        self._by_youtube_id: dict[str, Track] = {}
        self._by_spotify_uri: dict[str, Track] = {}
        self._by_isrc: dict[str, Track] = {}
        self._by_song: dict[tuple[str, str], list[Track]] = {}
        for row in rows:
            for index, key in ((self._by_youtube_id, row.youtube_id), (self._by_spotify_uri, row.spotify_uri),
                               (self._by_isrc, normalise_isrc(row.isrc))):
                if key and _better(index.get(key), row):
                    index[key] = row
            for key in _row_song_keys(row.title, row.artist, row.album_artist):
                songs = self._by_song.setdefault(key, [])
                if row not in songs:
                    songs.append(row)
        for songs in self._by_song.values():
            songs.sort(key=lambda row: row.status != "done")

    def by_source_id(self, keys: TrackKeys) -> Track | None:
        """The row a provider id names: the only match an album download may renumber."""
        return ((keys.spotify_uri and self._by_spotify_uri.get(keys.spotify_uri))
                or (keys.youtube_id and self._by_youtube_id.get(keys.youtube_id)) or None)

    def find(self, keys: TrackKeys) -> Track | None:
        if row := self.by_source_id(keys):
            return row
        if (isrc := normalise_isrc(keys.isrc)) and (row := self._by_isrc.get(isrc)):
            return row
        song = _song_key(keys.title, keys.artist)
        albums = (keys.album, *keys.album_aliases)
        return next((row for row in self._by_song.get(song, []) if song[0] and not _isrc_conflict(isrc, row)
                     and any(_same_album(album, row.album) for album in albums)), None)


async def library_index(session: AsyncSession, wanted: list[TrackKeys]) -> LibraryIndex:
    """Every row any of `wanted` may already be; song keys are normalised in Python, so all titles are scanned."""
    clauses = []
    if youtube_ids := {k.youtube_id for k in wanted if k.youtube_id}:
        clauses.append(Track.youtube_id.in_(youtube_ids))
    if spotify_uris := {k.spotify_uri for k in wanted if k.spotify_uri}:
        clauses.append(Track.spotify_uri.in_(spotify_uris))
    if isrcs := {normalise_isrc(k.isrc) for k in wanted if normalise_isrc(k.isrc)}:
        clauses.append(Track.isrc.in_(isrcs))
    songs = {key for k in wanted if (key := _song_key(k.title, k.artist))[0]}
    titles = await session.execute(select(Track.id, Track.title, Track.artist, Track.album_artist))
    song_ids = [row.id for row in titles if _row_song_keys(row.title, row.artist, row.album_artist) & songs]
    if song_ids:
        clauses.append(Track.id.in_(song_ids))
    if not clauses:
        return LibraryIndex([])
    return LibraryIndex(list((await session.scalars(select(Track).where(or_(*clauses)))).all()))


async def library_album(session: AsyncSession, album: str, artist: str) -> list[Track]:
    """The library's songs of `album` by `artist` (album artist or track artist), in disc and track order."""
    wanted_album, wanted_artist = normalize_title(album), _primary_artist(artist)
    if not wanted_album:
        return []
    candidates = await session.execute(select(Track.id, Track.album, Track.artist, Track.album_artist))
    ids = [row.id for row in candidates
           if normalize_title(row.album) == wanted_album
           and (not wanted_artist or wanted_artist in {_primary_artist(row.artist), _primary_artist(row.album_artist)})]
    if not ids:
        return []
    rows = await session.scalars(select(Track).where(Track.id.in_(ids)))
    return sorted(rows.all(), key=lambda t: (t.disc_number or 1, t.track_number or 0, t.title.casefold()))
