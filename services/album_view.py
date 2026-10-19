"""The local album view: a library group merged with its stored release, the template context
built from it, and the downloads it offers. Reads only, except for rows an explicit Download creates."""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import AlbumInfo, Track
from services import album_info, library_match, local_albums
from services.album_merge import MergedRow, artist_ok, ef, merge, primary
from services.artwork import album_cover_path, library_art_url, sidecar_art_path
from services.local_albums import LOOSE_KEY, LocalAlbum
from services.tracks import upsert_track

QUEUED_STATUSES = ("pending", "downloading")


@dataclass
class ViewRow:
    merged: MergedRow
    state: str                              # local | queued | error | missing | elsewhere
    track: Track | None = None              # the library row shown (this group's, or the find_song hit)
    match: str | None = None
    elsewhere: dict | None = None


@dataclass
class GroupView:
    album: LocalAlbum
    rows: list[Track]
    info: AlbumInfo | None
    release: list[dict] | None
    mismatch: bool
    view_rows: list[ViewRow] = field(default_factory=list)

    @property
    def downloads_allowed(self) -> bool:
        return self.info is not None and self.info.status == "found" and self.release is not None and not self.mismatch

    def at(self, disc: int, position: int) -> ViewRow | None:
        return next((row for row in self.view_rows if row.merged.release
                     and (row.merged.release["disc"], row.merged.release["position"]) == (disc, position)), None)


def _state_of(track: Track) -> str:
    if track.status == "done":
        return "local"
    return "queued" if track.status in QUEUED_STATUSES else "error"


def _artists(album: LocalAlbum, rows: list[Track], info: AlbumInfo | None) -> set[str]:
    names = {album.artist, album.album_artist, info.artist if info else None}
    for row in rows:
        names |= {row.artist, row.album_artist}
    return {name for name in names if name}


async def _resolve_candidates(session: AsyncSession, key: str, merged: list[MergedRow], artists: set[str]) -> list[ViewRow]:
    """A release track no merge pass placed is offered for download only when the library has
    that song under no album at all; a song this album has under another title shows as its own."""
    candidates = [row for row in merged if row.track is None]
    index = await library_match.song_index(session, {ef(row.release["title"]) for row in candidates})
    claimed: set[int] = set()
    view_rows: dict[int, ViewRow] = {}
    for row in candidates:
        hit = index.find_song(row.release["title"], artists | {row.release.get("artist") or ""},
                              row.release.get("duration_ms"))
        hit_key = local_albums.key_of(hit.album, hit.album_artist, hit.artist) if hit else None
        if hit is None:
            view_rows[id(row)] = ViewRow(merged=row, state="missing")
        elif hit_key == key:
            claimed.add(hit.id)
            view_rows[id(row)] = ViewRow(merged=row, state=_state_of(hit), track=hit, match="library")
        else:
            view_rows[id(row)] = ViewRow(merged=row, state="elsewhere", track=hit,
                                         elsewhere={"album": hit.album or "", "key": hit_key})
    result = []
    for row in merged:
        if row.track is None:
            result.append(view_rows[id(row)])
        elif not (row.on_release is False and row.track.id in claimed):
            result.append(ViewRow(merged=row, state=_state_of(row.track), track=row.track, match=row.match))
    return result


async def load_group(session: AsyncSession, key: str) -> GroupView | None:
    loaded = await local_albums.load(session, key)
    if loaded is None:
        return None
    album, rows = loaded
    info = None if key == LOOSE_KEY else await session.scalar(select(AlbumInfo).where(AlbumInfo.album_key == key))
    release = album_info.release_tracks(info)
    view = GroupView(album=album, rows=rows, info=info, release=release, mismatch=album_info.mismatched(info, rows))
    merged = merge(rows, release)
    if release is None:
        view.view_rows = [ViewRow(merged=row, state=_state_of(row.track), track=row.track) for row in merged]
    else:
        view.view_rows = await _resolve_candidates(session, key, merged, _artists(album, rows, info))
    return view


# --- template context ---


def _urls(key: str) -> dict:
    base = f"/api/albums/{key}"
    return {"self": f"{base}/html", "ensure": f"{base}/info/ensure", "status": f"{base}/info/status",
            "refresh_info": f"{base}/info/refresh", "reject_info": f"{base}/info/reject", "pin_info": f"{base}/info/pin",
            "download_missing": f"{base}/download-missing", "cover": f"{base}/cover"}


def _track_dict(view: GroupView, row: ViewRow) -> dict:
    release, track = row.merged.release, row.track
    own = track if row.state != "elsewhere" else None
    download_url = None
    if view.downloads_allowed and release and (row.state == "missing" or row.state == "error"):
        download_url = f"/api/albums/{view.album.key}/download/{release['disc']}/{release['position']}"
    return {
        "name": own.title if own else release["title"],
        "artist": own.artist if own else (release.get("artist") or view.album.artist),
        "disc_number": release["disc"] if release else ((own.disc_number or 1) if own else 1),
        "track_number": own.track_number if own else release["position"],
        "release_position": release["position"] if release else None,
        "duration_ms": (own.duration_ms if own and own.duration_ms else None) or (release or {}).get("duration_ms"),
        "isrc": (own.isrc if own else None) or (release or {}).get("isrc"),
        "state": row.state,
        # The Download button's answer says "Queued"; a reopened view must not call the same state "pending".
        "download_status": "queued" if track and track.status == "pending" else (track.status if track else None),
        "db": own.quality_readout() if own else None,
        "track_id": own.id if own else None,
        "on_release": row.merged.on_release,
        "match": row.match,
        "elsewhere": row.elsewhere,
        "download_url": download_url,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _info_dict(view: GroupView, status_override: str | None) -> dict:
    info, key = view.info, view.album.key
    can_enrich = view.album.kind == "album"
    pending = album_info.pending_for(key) is not None
    if not can_enrich:
        status = "never"
    elif info is None:
        status = "none" if settings.album_info_lookups else "disabled"
    else:
        status = info.status
    found = info is not None and info.status == "found"
    source = info.source if found else None
    source_url = None
    if source == "musicbrainz":
        source_url = f"https://musicbrainz.org/release/{info.source_id}"
    elif source == "deezer":
        source_url = f"https://www.deezer.com/album/{info.source_id}"
    should_ensure = can_enrich and album_info.lookups_open() and not pending and album_info.is_due(info, view.mismatch)
    return {
        # A view that starts the lookup on load already says so; the status poll only reports completion.
        "status": status_override or status, "pending": pending or should_ensure, "should_ensure": should_ensure,
        "can_enrich": can_enrich, "mismatch": view.mismatch, "pinned": bool(found and info.pinned),
        "artist_warning": bool(found and info.pinned and not artist_ok(
            info.artist, {primary(name) for name in _artists(view.album, view.rows, None)})),
        "source": source, "source_label": album_info.SOURCE_LABELS.get(source or ""), "source_url": source_url,
        "confidence": info.confidence if found else None, "fetched_at": _iso(info.fetched_at) if info else None,
        "retry_at": _iso(info.retry_at) if info else None, "release_title": info.title if found else None,
    }


def _genres(view: GroupView) -> list[str]:
    if view.info is not None and view.info.status == "found" and view.info.genres_json:
        try:
            return [str(g) for g in json.loads(view.info.genres_json)]
        except ValueError:
            pass
    return [g.strip() for g in (view.album.genre or "").split(",") if g.strip()]


def _album_dict(view: GroupView, tracks: list[dict]) -> dict:
    album, info = view.album, view.info
    found = info if info is not None and info.status == "found" else None
    done = [row for row in view.rows if row.status == "done"]
    complete = album.total_tracks is not None and len(done) >= album.total_tracks
    local_duration = (sum(row.duration_ms or 0 for row in done) or None) if complete else None
    if album.kind == "loose":
        total_discs = 1
    elif found:
        total_discs = max([found.total_discs or 1, *(t["disc_number"] for t in tracks)])
    else:
        total_discs = album.total_discs
    return {
        "key": album.key, "kind": album.kind, "name": album.name, "artist": album.artist,
        "album_artist": album.album_artist, "year": album.year or (found.year if found else None),
        "release_date": found.release_date if found else None, "label": found.label if found else None,
        "catalog_number": found.catalog_number if found else None, "barcode": found.barcode if found else None,
        "media_format": found.media_format if found else None,
        "total_tracks": (found.total_tracks if found else None) or album.total_tracks,
        "total_discs": total_discs,
        "total_duration_ms": (found.total_duration_ms if found else None) or local_duration,
        "local_count": (sum(t["on_release"] and t["state"] == "local" for t in tracks)
                        if view.release is not None and not view.mismatch else len(done)),
        "missing_count": sum(t["state"] == "missing" for t in tracks),
        "elsewhere_count": sum(t["state"] == "elsewhere" for t in tracks),
        "bonus_count": sum(t["on_release"] is False for t in tracks),
        "image_url": (f"/api/albums/{album.key}/cover?t={album.cover_track_id}"
                      if album.kind == "album" and album.cover_track_id else None),
        "genres": _genres(view),
    }


def bulk_download_allowed(view: GroupView) -> bool:
    """"Download missing" needs a confident (or pinned) match and enough local songs to trust it."""
    if not view.downloads_allowed or not any(row.state == "missing" for row in view.view_rows):
        return False
    local_count = sum(row.status == "done" for row in view.rows)
    if local_count <= 2 and view.album.total_tracks is None:
        return False
    return (view.info.confidence or 0) >= album_info.BULK_DOWNLOAD or bool(view.info.pinned)


def context(view: GroupView, *, album_id: str | None = None, notice: str | None = None,
            retry_url: str | None = None, status_override: str | None = None) -> dict:
    """The partials/album_view.html context."""
    key = view.album.key
    tracks = [_track_dict(view, row) for row in view.view_rows]
    album = _album_dict(view, tracks)
    info = _info_dict(view, status_override)
    downloads_allowed = view.downloads_allowed
    done = [row for row in view.rows if row.status == "done"]
    notices = [text for text in (notice, album_info.pop_notice(key)) if text]
    ctx = {
        "key": key, "provider": "library", "album_id": album_id or key, "album": album, "tracks": tracks,
        "multi_disc": album["total_discs"] > 1, "info": info, "downloads_allowed": downloads_allowed,
        "bulk_download_allowed": bulk_download_allowed(view), "urls": _urls(key), "upgrade_track_ids": [row.id for row in done],
        "upgrade_possible": any(row.upgrade_eligibility(include_unmatched=True) is None for row in done),
    }
    if notices:
        ctx["notice"] = " ".join(notices)
    if retry_url:
        ctx["retry_url"] = retry_url
    return ctx


async def build_context(session: AsyncSession, key: str, *, album_id: str | None = None, notice: str | None = None,
                        retry_url: str | None = None, status_override: str | None = None) -> dict | None:
    view = await load_group(session, key)
    if view is None:
        return None
    return context(view, album_id=album_id, notice=notice, retry_url=retry_url, status_override=status_override)


def album_detail_url(provider: str, album_id: str, name: str = "", artist: str = "") -> str:
    """Server-side twin of the album_detail_url macro."""
    url = f"/api/music/album/{quote(provider, safe='/')}/{quote(album_id, safe='/')}"
    if name or artist:
        url += "?" + urlencode({"name": name or "", "artist": artist or ""})
    return url


# --- downloads ---


def _uses_release_numbering(view: GroupView) -> bool:
    release = view.release or []
    if max((t["disc"] for t in release), default=1) == 1 or view.album.total_discs > 1:
        return True
    matched = [row for row in view.view_rows if row.match in ("isrc", "title", "position") and row.track]
    return bool(matched) and all(
        ((row.track.disc_number or 1), row.track.track_number) == (row.merged.release["disc"], row.merged.release["position"])
        for row in matched) and any(row.merged.release["disc"] > 1 for row in matched)


def _artwork_url(view: GroupView) -> str | None:
    """A sibling's sidecar, else the album's cached cover, else a provider cover URL. Blocking."""
    for row in view.rows:
        if row.status == "done" and sidecar_art_path(row.file_path):
            return library_art_url(row.id)
    if album_cover_path(view.album.key).is_file():
        return f"/api/albums/{view.album.key}/cover"
    return next((row.artwork_url for row in view.rows if row.artwork_url), None)


def _most_common(values):
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


async def new_track_data(view: GroupView, item: dict) -> dict:
    """The row a Download creates: the group's names, numbering that cannot split the album's
    card folder, the album's cover. Client input never reaches it."""
    release = sorted(view.release or [], key=lambda t: (t["disc"], t["position"]))
    album = view.album
    if _uses_release_numbering(view):
        disc, number = item["disc"], item["position"]
        discs = album.total_discs_known or max((t["disc"] for t in release), default=1)
        tracks = album.total_tracks or sum(t["disc"] == item["disc"] for t in release)
    else:
        disc, number = 1, next(i for i, t in enumerate(release, 1) if t == item)
        discs = album.total_discs_known or 1
        tracks = album.total_tracks or len(release)
    release_artist = (view.info.artist if view.info else "") or ""
    same_credit = ef(item.get("artist") or release_artist) == ef(release_artist)
    return {
        "title": item["title"], "isrc": item.get("isrc"), "duration_ms": item.get("duration_ms"),
        "artist": album.artist_spelling if same_credit or not item.get("artist") else item["artist"],
        "album": album.name, "album_artist": album.artist or None,
        "track_number": number, "disc_number": disc, "total_discs": discs, "total_tracks": tracks,
        "year": album.year or (view.info.year if view.info else None),
        "genre": _most_common(row.genre for row in view.rows if row.status == "done"),
        "artwork_url": await asyncio.to_thread(_artwork_url, view),
    }


async def queue_row(session: AsyncSession, view: GroupView, row: ViewRow, quality: str, container: str,
                    enqueue) -> dict:
    """Queue one album-view row: a missing song becomes a new row, this group's failed row is retried as is."""
    if row.state in ("local", "elsewhere"):
        return {"status": "already_downloaded", "track_id": row.track.id}
    if row.state == "queued":
        return {"status": "already_queued", "track_id": row.track.id}
    if row.state == "error":
        track = await upsert_track(session, {}, existing=row.track, quality=quality, container=container,
                                   source=row.track.source)
    else:
        track = await upsert_track(session, await new_track_data(view, row.merged.release), quality=quality,
                                   container=container, source=view.info.source)
    await enqueue(track.id)
    return {"status": "queued", "track_id": track.id}
