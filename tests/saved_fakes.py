"""Scripted stand-ins for the Saved-library refresh jobs: a spotipy client, a Qobuz client and seeding helpers.
No network: every answer is built here."""

from dataclasses import dataclass, field

import spotipy
from sqlalchemy import select

from models import Track, utc_now
from models_saved import SavedEntry, SavedItem, SavedSyncState
from services import spotify_client as sp
from services.qobuz_client import QobuzAlbum, QobuzArtist, QobuzPage, QobuzPlaylist
from services.saved_store import SavedData, replace_entries, upsert_items
from tests.qobuz_fakes import qtrack


def sp_track(n: int, *, isrc: str | None = None, artist: str = "Artist", album: str = "Album") -> dict:
    t = {"id": f"t{n:02d}", "uri": f"spotify:track:t{n:02d}", "name": f"Song {n}", "type": "track",
         "artists": [{"id": "ar1", "name": artist}], "duration_ms": 200000, "track_number": n, "disc_number": 1,
         "album": {"id": "al1", "name": album, "artists": [{"name": artist}], "release_date": "2004",
                   "images": [{"url": "https://i.scdn.co/image/abc"}]}}
    if isrc:
        t["external_ids"] = {"isrc": isrc}
    return t


def liked(n: int, *, day: int | None = None, shape: str = "track", **extra) -> dict:
    return {"added_at": f"2026-09-{day or (28 - n % 28):02d}T10:00:00Z", shape: sp_track(n, **extra)}


def sp_page(items: list, total: int, offset: int, limit: int = 50) -> dict:
    more = offset + len(items) < total
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "next": "https://api.spotify.com/v1/next" if more else None}


def paged(items: list, total: int | None = None, limit: int = 50) -> dict[int, dict]:
    total = len(items) if total is None else total
    pages = {offset: sp_page(items[offset:offset + limit], total, offset, limit) for offset in range(0, len(items), limit)}
    pages.setdefault(0, sp_page([], total, 0, limit))
    return pages


def rate_limit_error() -> spotipy.SpotifyException:
    return spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "7200"})


class FakeSpotify:
    """Answers library calls from pages keyed by offset; counts every call like the real guard does."""

    def __init__(self, *, tracks=None, albums=None, playlists=None, playlist_items=None, follow=None,
                 contains=None, me="me1") -> None:
        self.tracks, self.albums, self.playlists = tracks or {}, albums or {}, playlists or {}
        self.items_by_playlist = playlist_items or {}
        self.follow = follow
        self.contains = contains
        self.me = me
        self.calls: list[tuple] = []

    def _answer(self, name, pages, offset, limit):
        sp._count_call()
        self.calls.append((name, offset, limit))
        if limit == 1 and offset == 0 and 0 in pages and not isinstance(pages[0], Exception):
            head = dict(pages[0])
            return {**head, "items": head["items"][:1]}
        answer = pages.get(offset, sp_page([], (pages.get(0) or {}).get("total", 0), offset))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def current_user_saved_tracks(self, limit=20, offset=0, market=None):
        return self._answer("tracks", self.tracks, offset, limit)

    def current_user_saved_albums(self, limit=20, offset=0, market=None):
        return self._answer("albums", self.albums, offset, limit)

    def current_user_playlists(self, limit=50, offset=0):
        return self._answer("playlists", self.playlists, offset, limit)

    def playlist_items(self, playlist_id, fields=None, limit=50, offset=0, market=None, additional_types=()):
        sp._count_call()
        self.calls.append(("playlist_items", playlist_id, fields))
        answer = self.items_by_playlist[playlist_id]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def current_user(self):
        sp._count_call()
        self.calls.append(("me",))
        return {"id": self.me}

    def current_user_followed_artists(self, limit=20, after=None):
        sp._count_call()
        self.calls.append(("follow", after, limit))
        if isinstance(self.follow, Exception):
            raise self.follow
        return self.follow or {"artists": {"items": [], "total": 0, "next": None, "cursors": {"after": None}}}

    def current_user_saved_tracks_contains(self, tracks=None):
        sp._count_call()
        self.calls.append(("contains", tuple(tracks)))
        if isinstance(self.contains, Exception):
            raise self.contains
        return [t in (self.contains or set()) for t in tracks]


def sp_playlist(pid: str, *, owner: str = "me1", snapshot: str = "s1", total: int = 1) -> dict:
    return {"id": pid, "uri": f"spotify:playlist:{pid}", "name": f"List {pid}", "owner": {"id": owner, "display_name": owner},
            "snapshot_id": snapshot, "tracks": {"total": total}, "images": []}


def qalbum(n: int, **overrides) -> QobuzAlbum:
    values = {"id": f"qa{n}", "title": f"Album {n}", "version": None, "artist": "Miles Davis", "upc": None,
              "tracks_count": 2, "media_count": 1, "duration_s": 400, "year": 1959, "max_bit_depth": 24,
              "max_sampling_rate": 96.0, "streamable": True, "hires_streamable": True, "cover_url": None,
              "favorited_at": 1_760_000_000 - n}
    values.update(overrides)
    return QobuzAlbum(**values)


def qpage(items: list, total: int | None = None, offset: int = 0) -> QobuzPage:
    return QobuzPage(items=items, total=len(items) if total is None else total, offset=offset, limit=500,
                     raw_count=len(items))


@dataclass
class FakeQobuzLibrary:
    """Answers QobuzClient's library readers from scripted favorites; errors keyed by method name."""
    tracks: list = field(default_factory=list)
    albums: list = field(default_factory=list)
    artists: list = field(default_factory=list)
    playlists: list = field(default_factory=list)
    playlist_tracks: dict = field(default_factory=dict)
    ids: dict | None = None
    errors: dict = field(default_factory=dict)
    user_id: str = "42"
    rotated_token: str | None = None
    calls: list = field(default_factory=list)
    closed: bool = False

    def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]

    def _items(self, kind):
        return {"tracks": self.tracks, "albums": self.albums, "artists": self.artists}[kind]

    def favorites(self, kind, *, limit=500, offset=0):
        self._maybe_raise(f"favorites:{kind}:{limit}")
        items = self._items(kind)
        return qpage(items[offset:offset + limit], len(items), offset)

    def iter_favorites(self, kind, *, offset=0, page_cap=50):
        self._maybe_raise(f"iter:{kind}")
        yield qpage(self._items(kind), offset=offset)

    def favorite_ids(self):
        self._maybe_raise("ids")
        if self.ids is not None:
            return self.ids
        return {kind: [i.id for i in self._items(kind)] for kind in ("tracks", "albums", "artists")}

    def get_track(self, track_id):
        self._maybe_raise(f"get_track:{track_id}")
        return next(t for t in self.tracks if t.id == track_id)

    def get_album(self, album_id, *, page_cap=50):
        self._maybe_raise(f"get_album:{album_id}")
        album = next(a for a in self.albums if a.id == album_id)
        return album, [qtrack(id=f"{album_id}-1", album_id=album_id, album_title=album.title)]

    def iter_user_playlists(self, *, offset=0, page_cap=50):
        self._maybe_raise("playlists")
        yield qpage(self.playlists)

    def iter_playlist_tracks(self, playlist_id, *, offset=0, page_cap=50):
        self._maybe_raise(f"playlist:{playlist_id}")
        yield qpage(self.playlist_tracks.get(playlist_id, []))

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def qplaylist(pid: str, *, updated: int = 1, count: int = 1) -> QobuzPlaylist:
    return QobuzPlaylist(id=pid, name=f"Q list {pid}", tracks_count=count, owner_id="42", owner_name="me",
                         updated_at=updated, is_public=False, is_collaborative=False)


def qartist(n: int) -> QobuzArtist:
    return QobuzArtist(id=f"qr{n}", name=f"Artist {n}", albums_count=3, image_url=None, favorited_at=None)


async def items(factory, **where) -> list[SavedItem]:
    async with factory() as session:
        stmt = select(SavedItem).order_by(SavedItem.position, SavedItem.id)
        for name, value in where.items():
            stmt = stmt.where(getattr(SavedItem, name) == value)
        return list((await session.scalars(stmt)).all())


async def entries(factory, collection_id: int) -> list[SavedEntry]:
    async with factory() as session:
        return list((await session.scalars(select(SavedEntry).where(SavedEntry.collection_id == collection_id)
                                           .order_by(SavedEntry.position))).all())


async def state(factory, source: str, kind: str) -> SavedSyncState | None:
    async with factory() as session:
        return await session.scalar(select(SavedSyncState).where(SavedSyncState.source == source,
                                                                 SavedSyncState.kind == kind))


async def add_track(factory, **fields) -> Track:
    values = {"title": "Song 1", "artist": "Artist", "album": "Album", "status": "done", "format": "mp3",
              "quality": "mp3_320"}
    values.update(fields)
    async with factory() as session:
        track = Track(**values)
        session.add(track)
        await session.commit()
        return track


def saved_song(n: int, **extra) -> SavedData:
    return SavedData(source_id=f"t{n}", uri=f"spotify:track:t{n}", title=f"Song {n}", artist="Artist", album="Album",
                     duration_ms=240_000, **extra)


async def seed_saved(factory, source: str, kind: str, rows: list[SavedData]) -> list[SavedItem]:
    async with factory() as session:
        await upsert_items(session, source, kind, rows, now=utc_now())
        await session.commit()
        return list((await session.scalars(select(SavedItem).where(SavedItem.source == source, SavedItem.kind == kind)
                                           .order_by(SavedItem.position))).all())


async def seed_collection(factory, source: str, kind: str, entries: list[SavedData], *, source_id: str = "c1",
                          **fields) -> SavedItem:
    async with factory() as session:
        await upsert_items(session, source, kind, [SavedData(source_id=source_id, title="Collection", **fields)],
                           now=utc_now())
        item = await session.scalar(select(SavedItem).where(SavedItem.source == source, SavedItem.kind == kind,
                                                            SavedItem.source_id == source_id))
        await replace_entries(session, item, entries, tag=None, now=utc_now())
        await session.commit()
        return item
