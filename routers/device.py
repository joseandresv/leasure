import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session, get_session
from models import CardCopy, Playlist, PlaylistTrack, SupersededFile, SyncHistory, Track, utc_now
from services import wsl_mount
from services.device import build_device_path, detect_devices, detect_stale_wsl2, resolve_sync_target
from services.platform import device_path_placeholder, get_platform
from services.playlist import (
    generate_m3u,
    record_generated_playlists,
    sanitize_playlist_stem,
    sweep_orphan_playlists,
)

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["platform"] = get_platform()
templates.env.globals["device_path_placeholder"] = device_path_placeholder()

DEVICE_AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ape", ".dsf", ".dff", ".ogg", ".m4a", ".opus"}
SYSTEM_FOLDER_NAMES = ("$RECYCLE.BIN", "System Volume Information", ".Trash-1000")


def _is_accessible(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_writable(path: Path) -> bool:
    # drvfs reports 0777 even on a write-protected card, so only a real write tells.
    probe = path / f".leasure-write-test-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"leasure")
    except OSError as e:
        logger.warning("Write test on %s failed: %s", path, e)
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove the write test file %s: %s", probe, e)
    return True


def _msg(text: str, color: str = "--text-secondary") -> HTMLResponse:
    return HTMLResponse(f'<p style="color:var({color});">{escape(text)}</p>')


def _mount_result_response(request: Request, result: wsl_mount.MountResult) -> HTMLResponse:
    """Every mount/eject outcome, errors included, tells the page the drive list may have changed."""
    response = templates.TemplateResponse(
        request=request, name="partials/mount_result.html",
        context={"status": result.status, "message": result.message, "letter": result.letter,
                 "manual": result.manual},
    )
    response.headers["HX-Trigger"] = wsl_mount.HX_DRIVES_CHANGED
    return response


@router.post("/mount")
async def mount_drive(request: Request, letter: str = Form("")):
    """Mount a Windows drive letter in WSL2 via drvfs. No-op elsewhere: the OS auto-mounts."""
    if get_platform() != "wsl2":
        return _mount_result_response(request, wsl_mount.MountResult(
            "unsupported", "Removable drives mount automatically on this OS. Plug in the device and click Scan."))
    return _mount_result_response(request, await asyncio.to_thread(wsl_mount.mount, letter))


def _sync_running_to(mount_point: str) -> bool:
    return mount_point in _LEGACY_SYNC_TARGETS or any(
        job["device_path"] == mount_point and not job["task"].done() for job in _SYNC_JOBS.values())


# Mount points an eject is unmounting right now; a sync must not start on one.
_EJECTING: set[str] = set()


@router.post("/eject")
async def eject_drive(request: Request, letter: str = Form("")):
    """Unmount /mnt/<letter> so the card can be pulled. Refused while a sync writes to it."""
    if get_platform() != "wsl2":
        return _mount_result_response(request, wsl_mount.MountResult(
            "unsupported", "Use Safely Remove Hardware or your file manager to eject the card."))
    wanted = letter.strip().lower()
    if not wsl_mount.LETTER_RE.fullmatch(wanted):
        return _mount_result_response(request, await asyncio.to_thread(wsl_mount.unmount, letter))
    mount_point = f"/mnt/{wanted}"
    # Check and mark with no await in between, so a sync cannot start in the gap.
    if _sync_running_to(mount_point):
        return _mount_result_response(request, wsl_mount.MountResult(
            "busy_sync", f"A sync to {wanted.upper()}: is still running. Wait for it to finish.", wanted))
    if mount_point in _EJECTING:
        return _mount_result_response(request, wsl_mount.MountResult(
            "busy", f"{wanted.upper()}: is already being ejected.", wanted))
    _EJECTING.add(mount_point)
    try:
        return _mount_result_response(request, await asyncio.to_thread(wsl_mount.unmount, wanted))
    finally:
        _EJECTING.discard(mount_point)


def _device_list_context(mount_results: list[wsl_mount.MountResult] | None = None) -> dict:
    """Detected volumes plus, on WSL2, the stale mounts (thread-side). Hand-mount commands only
    when automount needed them; otherwise the Card Reader is the one place that offers them."""
    wsl2 = get_platform() == "wsl2"
    return {
        "devices": detect_devices(),
        "stale": detect_stale_wsl2() if wsl2 else [],
        "manual": next((r.manual for r in mount_results or [] if r.manual), None),
    }


@router.get("/detect")
async def detect():
    return await asyncio.to_thread(detect_devices)


@router.get("/detect/html")
async def detect_html(request: Request):
    return templates.TemplateResponse(request=request, name="partials/device_list.html",
                                      context=await asyncio.to_thread(_device_list_context))


@router.post("/automount")
async def automount_html(request: Request):
    """Mount any inserted card (WSL2), then render the device list. Elsewhere it is a plain detect."""
    if get_platform() != "wsl2":
        return await detect_html(request)
    try:
        mount_results = await asyncio.to_thread(wsl_mount.automount)
    except Exception:
        # The device list is the panel's only content on WSL2; it must render whatever happened.
        logger.exception("Automount failed")
        mount_results = []
    context = await asyncio.to_thread(_device_list_context, mount_results)
    response = templates.TemplateResponse(request=request, name="partials/device_list.html",
                                          context={**context, "mount_results": mount_results})
    if mount_results:
        response.headers["HX-Trigger"] = wsl_mount.HX_DRIVES_CHANGED
    return response


def _drive_state(disk: dict, mount_state: str | None) -> str:
    if mount_state == "stale" and not disk["has_media"]:
        return "stale"
    if not disk["has_media"]:
        return "no_media"
    return "mounted" if mount_state == "ok" else "unmounted"


def _gib(size: int | None) -> float | None:
    # The unit detect_devices() uses for total_gb, so one card shows one size on the page.
    return round(size / 1024**3, 1) if isinstance(size, int) and size > 0 else None


def _removable_drive_rows(listing: dict | None, states: dict[str, str]) -> list[dict]:
    rows = []
    for disk in wsl_mount.removable_drives(listing) if listing else []:
        total = disk["total_bytes"]
        rows.append({"letter": disk["letter"], "label": disk["label"], "fs": disk["fs"],
                     "total_bytes": total, "total_gb": _gib(total),
                     "state": _drive_state(disk, states.get(disk["letter"]))})
    listed = {row["letter"] for row in rows}
    system = listing["system"] if listing else "c"
    for letter, state in sorted(states.items()):
        if state == "stale" and letter not in listed and letter not in (system, "c"):
            rows.append({"letter": letter, "label": f"{letter.upper()}:", "fs": "", "total_bytes": None,
                         "total_gb": None, "state": "stale"})
    return rows


def _picker_letter(raw: str) -> str:
    letter = raw.strip().lower()
    return letter if wsl_mount.LETTER_RE.fullmatch(letter) and letter >= "d" else "e"


@router.get("/removable/html")
async def removable_drives_html(request: Request, letter: str = "e"):
    """The Windows card-reader slots with their mount state (WSL2 only). `letter` is the
    picker's choice, kept across refreshes and used for the hand-mount commands."""
    if get_platform() != "wsl2":
        return _msg("Removable drives mount automatically on this OS.")
    listing = await asyncio.to_thread(wsl_mount.list_windows_drives)
    states = await asyncio.to_thread(wsl_mount.mounted_letters)
    selected = _picker_letter(letter)
    return templates.TemplateResponse(
        request=request, name="partials/removable_drives.html",
        context={"drives": _removable_drive_rows(listing, states), "listing_ok": listing is not None,
                 "selected_letter": selected, "manual": wsl_mount.manual_commands(selected)},
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

CARD_ID_FILE = ".leasure-card.json"
_CARD_ID_RE = re.compile(r"[0-9A-Za-z-]{1,36}")
_PART_SUFFIX = ".leasure-part"
_HEAD_BYTES = 1024 * 1024
# FAT32 cannot hold a file of 4 GiB.
_MAX_CARD_FILE_BYTES = 4 * 1024**3 - 1
# Windows MAX_PATH counts the "X:\" drive prefix too.
_MAX_CARD_PATH = 255
_CARD_RESERVE_BYTES = 64 * 1024**2
# Assumed allocation unit when the card does not report one (FAT32's default for 16-32 GB cards).
_DEFAULT_CLUSTER_BYTES = 32 * 1024
_LISTED_PATHS = 50
_KEPT_REASONS = ("waiting", "path_in_use", "changed_on_card")
_KIND_SUFFIXES = {"audio": frozenset(DEVICE_AUDIO_EXTS), "jpg": frozenset({".jpg"}), "lrc": frozenset({".lrc"})}


async def _load_sync_tracks(session: AsyncSession, scope: str) -> list[Track]:
    stmt = select(Track).where(Track.status == "done")
    if scope == "new":
        stmt = stmt.where(Track.synced_at.is_(None))
    result = await session.execute(stmt)
    tracks = [t for t in result.scalars().all() if t.file_path]
    # The H2 may play a folder in the order its files were written, so copy album by album
    # in disc/track order.
    tracks.sort(key=_album_order_key)
    return tracks


def _device_rel_path(track: Track, ext: str) -> str:
    return build_device_path(track.artist, track.album, track.track_number, track.title, ext,
                             album_artist=track.album_artist, disc_number=track.disc_number,
                             total_discs=track.total_discs)


def _album_order_key(track: Track) -> tuple:
    album_dir = PurePosixPath(_device_rel_path(track, "x")).parent.as_posix().casefold()
    return (album_dir, track.disc_number or 1, track.track_number or 10**6, (track.title or "").casefold(), track.id)


# What a copy to the card was built from. An album download that changes any of these
# mid-sync must not have its synced_at=None overwritten by the sync.
_COPIED_FIELDS = ("file_path", "artist", "title", "album", "album_artist", "track_number", "disc_number",
                  "total_tracks", "total_discs")


async def _mark_synced(session: AsyncSession, copied: list[Track]) -> None:
    """Set synced_at on the copied rows that still hold the values the copy used, then forget them."""
    now = utc_now()
    for track in copied:
        unchanged = [getattr(Track, f).is_not_distinct_from(getattr(track, f)) for f in _COPIED_FIELDS]
        await session.execute(update(Track).where(Track.id == track.id, *unchanged).values(synced_at=now)
                              .execution_options(synchronize_session=False))
    copied.clear()


def _read_card_id(target: Path) -> str | None:
    try:
        data = json.loads((target / CARD_ID_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    card_id = data.get("card_id") if isinstance(data, dict) else None
    return card_id if isinstance(card_id, str) and _CARD_ID_RE.fullmatch(card_id) else None


def _card_identity(target: Path) -> str:
    """The card's id from .leasure-card.json at its root, created on the first sync (thread-side)."""
    card_id = _read_card_id(target)
    if card_id is None:
        card_id = uuid.uuid4().hex
        (target / CARD_ID_FILE).write_text(json.dumps({"card_id": card_id, "created_at": utc_now().isoformat()}),
                                           encoding="utf-8")
    return card_id


# FAT32 stores modification times in 2-second steps.
_MTIME_TOLERANCE_S = 2


def _is_current(src: Path, dst: Path) -> bool:
    """The card copy has the source's size and (FAT-rounded) mtime, so copying again changes nothing."""
    try:
        dst_stat = dst.stat()
    except OSError:
        return False
    src_stat = src.stat()
    return dst_stat.st_size == src_stat.st_size and abs(dst_stat.st_mtime - src_stat.st_mtime) <= _MTIME_TOLERANCE_S


def _copy_file(src: Path, dst: Path) -> bool:
    """Copy unless the card copy is current; True when bytes were written. The bytes land in a
    temporary file that replaces `dst` only once complete, so a failed copy keeps the old one."""
    if _is_current(src, dst):
        return False
    part = dst.with_name(dst.name + _PART_SUFFIX)
    part.unlink(missing_ok=True)
    try:
        # copyfile, not copy2: drvfs/FAT32 cannot take the permission bits copy2 sets
        shutil.copyfile(str(src), str(part))
        src_stat = src.stat()
        try:
            # The source mtime on the copy is what lets the next sync recognise it as current.
            os.utime(part, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
        except OSError as e:
            logger.debug("Could not set the mtime of %s: %s", part, e)
        os.replace(part, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    return True


@dataclass
class _Written:
    """One file a track sync left on the card: copied now, or already current."""
    kind: str
    rel_path: str
    src: Path
    copied: bool


def _track_files(src: Path, track: Track) -> list[tuple[str, Path, str]]:
    """(kind, library file, card rel path) of the audio file and the sidecars it has, audio first."""
    files = [("audio", src, _device_rel_path(track, src.suffix.lstrip(".")))]
    for kind in ("jpg", "lrc"):
        sidecar = src.with_suffix(f".{kind}")
        if sidecar.exists():
            files.append((kind, sidecar, _device_rel_path(track, kind)))
    return files


def _copy_track(src: Path, target: Path, track: Track) -> list[_Written]:
    """Copy the audio file plus its .jpg/.lrc sidecars, skipping files the card already
    holds unchanged. Runs in a worker thread. The audio file comes first."""
    files = _track_files(src, track)
    (target / files[0][2]).parent.mkdir(parents=True, exist_ok=True)
    return [_Written(kind, rel, file_src, _copy_file(file_src, target / rel)) for kind, file_src, rel in files]


def _head_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.sha256(f.read(_HEAD_BYTES)).hexdigest()


def _card_copy_values(written: list[_Written], track_id: int, card_id: str, known: dict) -> list[dict]:
    """CardCopy values for the files a track sync left on the card (thread-side). The head hash is
    reused while the source keeps the size and mtime of the stored row."""
    rows = []
    for w in written:
        st = w.src.stat()
        key = w.rel_path.casefold()
        prev = known.get(key)
        head = prev[2] if prev and prev[:2] == (st.st_size, st.st_mtime) else _head_sha256(w.src)
        known[key] = (st.st_size, st.st_mtime, head)
        rows.append({"card_id": card_id, "track_id": track_id, "kind": w.kind, "rel_path": w.rel_path,
                     "rel_key": key, "size": st.st_size, "mtime": st.st_mtime, "head_sha256": head})
    return rows


def _sync_track(src: Path, target: Path, track: Track, card_id: str, known: dict) -> tuple[int | None, list[dict]]:
    """Copy one track (thread-side): the audio bytes written (None when already current) and its CardCopy values."""
    written = _copy_track(src, target, track)
    audio = written[0]
    return (audio.src.stat().st_size if audio.copied else None), _card_copy_values(written, track.id, card_id, known)


async def _known_card_copies(session: AsyncSession, card_id: str) -> dict[str, tuple[int, float, str]]:
    rows = await session.execute(select(CardCopy.rel_key, CardCopy.size, CardCopy.mtime, CardCopy.head_sha256)
                                 .where(CardCopy.card_id == card_id))
    return {key: (size, mtime, head) for key, size, mtime, head in rows}


async def _record_card_copies(session: AsyncSession, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = sqlite_insert(CardCopy).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CardCopy.card_id, CardCopy.rel_key],
        set_={c: stmt.excluded[c] for c in ("track_id", "kind", "rel_path", "size", "mtime", "head_sha256")})
    await session.execute(stmt)
    rows.clear()


@dataclass
class _SpacePlan:
    """What a sync needs on the card before it copies anything, and the tracks it must skip."""
    free_bytes: int
    needed_bytes: int = 0
    too_large: list[str] = field(default_factory=list)
    too_long: list[str] = field(default_factory=list)
    skip: dict[int, str] = field(default_factory=dict)

    @property
    def fits(self) -> bool:
        return self.needed_bytes <= self.free_bytes

    def context(self) -> dict:
        return {"needed_bytes": self.needed_bytes, "free_bytes": self.free_bytes, "fits": self.fits,
                "too_large": self.too_large, "too_long": self.too_long}


def _free_bytes(target: Path) -> int:
    return shutil.disk_usage(str(target)).free


def _cluster_bytes(target: Path) -> int:
    """The card's allocation unit: every file and folder takes whole clusters (up to 128 KiB on exFAT)."""
    if hasattr(os, "statvfs"):
        st = os.statvfs(str(target))
        return st.f_frsize or st.f_bsize or _DEFAULT_CLUSTER_BYTES
    import ctypes

    sectors, sector_size, free, total = (ctypes.c_ulong() for _ in range(4))
    if ctypes.windll.kernel32.GetDiskFreeSpaceW(target.anchor or str(target), ctypes.byref(sectors),
                                                ctypes.byref(sector_size), ctypes.byref(free), ctypes.byref(total)):
        return sectors.value * sector_size.value or _DEFAULT_CLUSTER_BYTES
    return _DEFAULT_CLUSTER_BYTES


def _plan_space(target: Path, tracks: list[Track]) -> _SpacePlan:
    """Walk the files in sync order (thread-side). A replaced file still occupies the card while its
    .leasure-part copy is written, and the superseded copies the cleanup removes afterwards are not credited."""
    plan = _SpacePlan(free_bytes=_free_bytes(target))
    long_paths_fail = get_platform() in ("windows", "wsl2")
    cluster = _cluster_bytes(target)

    def on_card(size: int) -> int:
        return -(-size // cluster) * cluster

    new_dirs: set[str] = set()
    used = peak = 0
    for track in tracks:
        src = Path(track.file_path)
        if not src.is_file():
            continue
        files = _track_files(src, track)
        rel = files[0][2]
        if src.stat().st_size >= _MAX_CARD_FILE_BYTES:
            plan.too_large.append(rel)
            plan.skip[track.id] = "too large for the card"
            continue
        if long_paths_fail and len(rel) + 3 > _MAX_CARD_PATH:
            plan.too_long.append(rel)
            plan.skip[track.id] = "path too long for the card"
            continue
        for _, file_src, file_rel in files:
            dst = target / file_rel
            if _is_current(file_src, dst):
                continue
            for folder in PurePosixPath(file_rel).parents[:-1]:
                if folder.as_posix() not in new_dirs and not (target / folder).is_dir():
                    new_dirs.add(folder.as_posix())
                    used += cluster
            size = on_card(file_src.stat().st_size)
            peak = max(peak, used + size)
            used += size - (on_card(dst.stat().st_size) if dst.is_file() else 0)
    plan.needed_bytes = peak + _CARD_RESERVE_BYTES if peak else 0
    return plan


def _size_label(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{max(1, round(n / 1024**2))} MB"


@dataclass
class _SyncCounts:
    """Tracks now on the card: `copied` were written by this run, `skipped` were already current."""
    copied: int = 0
    skipped: int = 0
    total_size: int = 0

    @property
    def synced(self) -> int:
        return self.copied + self.skipped

    def add(self, written: int | None) -> None:
        if written is None:
            self.skipped += 1
        else:
            self.copied += 1
            self.total_size += written

    def history(self, target: Path, removed: int) -> SyncHistory:
        return SyncHistory(device_path=str(target), tracks_added=self.copied, tracks_skipped=self.skipped,
                           tracks_removed=removed, total_size=self.total_size)


@dataclass
class _SyncRun:
    """State of one sync that is committed in batches."""
    card_id: str
    known: dict
    counts: _SyncCounts = field(default_factory=_SyncCounts)
    errors: list[str] = field(default_factory=list)
    copied: list[Track] = field(default_factory=list)
    card_rows: list[dict] = field(default_factory=list)


async def _flush(session: AsyncSession, run: _SyncRun) -> None:
    await _mark_synced(session, run.copied)
    await _record_card_copies(session, run.card_rows)
    await session.commit()


@dataclass(frozen=True)
class _SupersededCopy:
    """A SupersededFile row and the track's current audio, as plain values for a worker thread."""
    id: int
    kind: str
    rel_path: str
    size: int
    mtime: float
    head_sha256: str
    title: str
    artist: str
    replacement: tuple[Path, str] | None  # library file and card rel path, when the track is done


def _superseded_on_card(card_id: str | None) -> tuple:
    """Records that may concern this card: its own, plus the card-independent library ones."""
    return (or_(SupersededFile.card_id.is_(None), SupersededFile.card_id == card_id),
            or_(SupersededFile.basis != "card_copy", SupersededFile.last_removed_at.is_(None)))


async def _load_superseded(session: AsyncSession, card_id: str | None) -> tuple[list[_SupersededCopy], set[str]]:
    """The records to judge, plus the casefolded card paths every library row maps to (the live set)."""
    records = (await session.scalars(select(SupersededFile).where(*_superseded_on_card(card_id))
                                     .order_by(SupersededFile.id))).all()
    if not records:
        return [], set()
    tracks = {t.id: t for t in (await session.scalars(select(Track).where(Track.file_path.is_not(None)))).all()}
    live = set()
    for t in tracks.values():
        ext = Path(t.file_path).suffix.lstrip(".")
        live.update(_device_rel_path(t, e).casefold() for e in (ext, "jpg", "lrc"))
    copies = []
    for rec in records:
        t = tracks.get(rec.track_id)
        replacement = ((Path(t.file_path), _device_rel_path(t, Path(t.file_path).suffix.lstrip(".")))
                       if t and t.status == "done" else None)
        copies.append(_SupersededCopy(rec.id, rec.kind, rec.rel_path, rec.size, rec.mtime, rec.head_sha256,
                                      t.title if t else "", t.artist if t else "", replacement))
    return copies, live


def _safe_card_rel(rel: str) -> bool:
    """Artist/Album/File as build_device_path makes it: relative, POSIX, nothing that climbs out."""
    parts = rel.split("/")
    return len(parts) == 3 and "\\" not in rel and ":" not in rel and all(p and p not in (".", "..") for p in parts)


def _is_link(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def _inside_real_dirs(target: Path, rel: str) -> bool:
    artist, album, _ = rel.split("/")
    if _is_link(target / artist) or _is_link(target / artist / album):
        return False
    return target.resolve() in (target / rel).resolve().parents


def _same_copy(path: Path, rec: _SupersededCopy) -> bool:
    """The bytes Leasure wrote: size, head hash, and the mtime it set, which FAT keeps in local time
    and so shifts by an hour across a DST change."""
    st = path.stat()
    if st.st_size != rec.size:
        return False
    if not any(abs(st.st_mtime - rec.mtime - k * 3600) <= _MTIME_TOLERANCE_S for k in (-1, 0, 1)):
        return False
    return _head_sha256(path) == rec.head_sha256


def _judge_superseded(target: Path, rec: _SupersededCopy, live: set[str]) -> str:
    """The verdict on one record: "ready" when its card copy may be deleted, else why not (thread-side)."""
    if not _safe_card_rel(rec.rel_path) or not _inside_real_dirs(target, rec.rel_path):
        return "unsafe"
    path = target / rec.rel_path
    if not path.is_symlink() and not path.exists():
        return "missing"
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _KIND_SUFFIXES.get(rec.kind, ()):
        return "unsafe"
    if rec.rel_path.casefold() in live:
        return "path_in_use"
    if not _same_copy(path, rec):
        return "changed_on_card"
    src, rel = rec.replacement or (None, None)
    if src is None or not src.is_file() or not _is_current(src, target / rel):
        return "waiting"
    return "ready"


def _remove_empty_dirs(target: Path, folder: Path) -> None:
    root = target.resolve()
    for d in (folder, folder.parent):
        if root not in d.resolve().parents:
            return
        try:
            d.rmdir()
        except OSError:
            return


def _sweep_superseded(target: Path, records: list[_SupersededCopy], live: set[str]) -> list[str]:
    """Judge every record and delete the ready copies (thread-side). One verdict per record:
    "removed", "duplicate" (an earlier record removed that path), "missing", "unsafe", "failed" or a kept reason."""
    verdicts = []
    removed: set[str] = set()
    for rec in records:
        key = rec.rel_path.casefold()
        verdict = "duplicate" if key in removed else _judge_superseded(target, rec, live)
        if verdict == "unsafe":
            logger.warning("Not removing %r from the card: unsafe path or not a regular %s file", rec.rel_path, rec.kind)
        elif verdict == "ready":
            path = target / rec.rel_path
            try:
                path.unlink()
            except OSError as e:
                logger.warning("Could not remove the superseded copy %s: %s", path, e)
                verdict = "failed"
            else:
                _remove_empty_dirs(target, path.parent)
                removed.add(key)
                verdict = "removed"
        verdicts.append(verdict)
    return verdicts


@dataclass
class _CleanupResult:
    removed_paths: list[str] = field(default_factory=list)
    kept_paths: list[dict] = field(default_factory=list)

    def event_fields(self) -> dict:
        return {"removed": len(self.removed_paths), "removed_paths": self.removed_paths[:_LISTED_PATHS],
                "kept": len(self.kept_paths), "kept_paths": self.kept_paths[:_LISTED_PATHS]}


async def _remove_superseded(target: Path, card_id: str, session: AsyncSession) -> _CleanupResult:
    """Final sync pass: delete the card copies an upgrade or revert made obsolete, but only a file whose
    bytes are the ones Leasure wrote and whose replacement is on the card. Everything else is kept and listed."""
    result = _CleanupResult()
    records, live = await _load_superseded(session, card_id)
    if not records:
        return result
    verdicts = await asyncio.to_thread(_sweep_superseded, target, records, live)
    now = utc_now()
    removed_keys = set()
    for rec, verdict in zip(records, verdicts, strict=True):
        if verdict in ("removed", "duplicate"):
            await session.execute(update(SupersededFile).where(SupersededFile.id == rec.id).values(last_removed_at=now))
        if verdict == "removed":
            key = rec.rel_path.casefold()
            removed_keys.add(key)
            result.removed_paths.append(rec.rel_path)
            await session.execute(delete(CardCopy).where(CardCopy.card_id == card_id, CardCopy.rel_key == key))
    await session.commit()
    kept: dict[str, dict] = {}
    for rec, verdict in zip(records, verdicts, strict=True):
        key = rec.rel_path.casefold()
        if verdict in _KEPT_REASONS and key not in removed_keys:
            kept.setdefault(key, {"path": rec.rel_path, "reason": verdict})
    result.kept_paths = list(kept.values())
    return result


async def _superseded_audio_by_track(session: AsyncSession, card_id: str) -> dict[int, list[str]]:
    rows = await session.execute(
        select(SupersededFile.track_id, SupersededFile.rel_path)
        .where(SupersededFile.kind == "audio", SupersededFile.track_id.is_not(None), *_superseded_on_card(card_id))
        .order_by(SupersededFile.id.desc()))
    by_track: dict[int, list[str]] = {}
    for track_id, rel in rows:
        if _safe_card_rel(rel):
            by_track.setdefault(track_id, []).append(rel)
    return by_track


def _generate_card_playlist(name: str, tracks: list[dict], target: Path) -> Path:
    """generate_m3u with each entry pointing at the copy really on the card: the current file, or the
    superseded one while its replacement could not be copied (thread-side)."""
    for track in tracks:
        fallbacks = track.pop("superseded_rel_paths")
        if not (target / track["rel_path"]).is_file():
            track["rel_path"] = next((rel for rel in fallbacks if (target / rel).is_file()), track["rel_path"])
    return generate_m3u(name, tracks, target)


async def _write_playlists(session: AsyncSession, target: Path, errors: list[str], card_id: str) -> int:
    """Regenerate the .m3u8 files for DB playlists at the card root and reclaim the
    ones Leasure wrote earlier that no longer exist. User-made playlists are left alone."""
    playlists_generated = 0
    try:
        playlist_result = await session.execute(select(Playlist))
        db_playlists = playlist_result.scalars().all()

        keep_stems = {sanitize_playlist_stem(pl.name) for pl in db_playlists}
        removed = await asyncio.to_thread(sweep_orphan_playlists, target, keep_stems)
        if removed:
            logger.info("Removed %d orphan playlist file(s): %s", len(removed), removed)

        superseded = await _superseded_audio_by_track(session, card_id)
        written: list[Path] = []
        for pl in db_playlists:
            entries = await session.execute(
                select(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id).order_by(PlaylistTrack.position)
            )
            seen_track_ids: set[int] = set()
            pl_tracks = []
            for entry in entries.scalars().all():
                if entry.track_id in seen_track_ids:
                    continue
                seen_track_ids.add(entry.track_id)
                t = await session.get(Track, entry.track_id)
                if t and t.status == "done" and t.file_path:
                    pl_tracks.append(
                        {"artist": t.artist, "album": t.album, "album_artist": t.album_artist,
                         "track_number": t.track_number, "disc_number": t.disc_number,
                         "total_discs": t.total_discs, "title": t.title, "format": t.format,
                         "duration_ms": t.duration_ms,
                         "rel_path": _device_rel_path(t, Path(t.file_path).suffix.lstrip(".")),
                         "superseded_rel_paths": superseded.get(t.id, [])}
                    )
            if pl_tracks:
                written.append(await asyncio.to_thread(_generate_card_playlist, pl.name, pl_tracks, target))
                playlists_generated += 1
        if written:
            await asyncio.to_thread(record_generated_playlists, target, written)
    except Exception as e:
        errors.append(f"Playlist generation error: {e}")
        logger.exception("Failed to generate playlists")
    return playlists_generated


@router.post("/sync")
async def sync_to_device(
    request: Request,
    device_path: str = Form(...),
    scope: str = Form("all"),
    session: AsyncSession = Depends(get_session),
):
    """Non-streaming sync (legacy HTML form)."""
    resolved = await asyncio.to_thread(resolve_sync_target, device_path)
    if not resolved or resolved in _EJECTING:
        error = (f"{device_path} is not a detected removable drive. Click Scan and pick the H2." if not resolved
                 else "The card is being ejected. Mount it again, then sync.")
        return templates.TemplateResponse(request=request, name="partials/sync_result.html", context={"error": error})
    events: list[dict] = []
    _LEGACY_SYNC_TARGETS.append(resolved)
    try:
        await _sync_library_to_device(session, Path(resolved), scope, events.append)
    finally:
        _LEGACY_SYNC_TARGETS.remove(resolved)
    result = events[-1]
    if result["type"] == "error":
        return templates.TemplateResponse(request=request, name="partials/sync_result.html",
                                          context={"error": result["message"]})
    return templates.TemplateResponse(request=request, name="partials/sync_result.html",
                                      context={
                                          "synced": result["synced"],
                                          "copied": result["copied"],
                                          "skipped": result["skipped"],
                                          "total_size_mb": result["size_mb"],
                                          "playlists_generated": result["playlists"],
                                          "errors": result["errors"],
                                          "removed": result["removed"],
                                          "removed_paths": result["removed_paths"],
                                          "kept": result["kept"],
                                          "kept_paths": result["kept_paths"],
                                      })


# A sync is created with POST (so a cross-site navigation or <img> cannot start one)
# and then observed with GET on its job id. Jobs are single-use and expire quickly.
_SYNC_JOBS: dict[str, dict] = {}
# Targets the non-streaming /sync is writing to (it runs inside its request, not as a job).
_LEGACY_SYNC_TARGETS: list[str] = []
_SYNC_JOB_TTL = 300  # seconds
_SYNC_COMMIT_EVERY = 25


def _prune_jobs() -> None:
    """Drop expired jobs and the events they never delivered. A job whose task is
    still copying is kept whatever its age, so the task is never left unreferenced."""
    now = time.monotonic()
    for job_id, job in list(_SYNC_JOBS.items()):
        if job["task"].done() and now - job["created"] > _SYNC_JOB_TTL:
            _SYNC_JOBS.pop(job_id, None)


async def _record_sync(session: AsyncSession, target: Path, run: _SyncRun, removed: int) -> None:
    try:
        await _flush(session, run)
        session.add(run.counts.history(target, removed))
        await session.commit()
    except Exception:
        logger.exception("Failed to record sync history")


def _done_event(counts: _SyncCounts, total: int, playlists: int, errors: list[str], cleanup: _CleanupResult) -> dict:
    return {"type": "done", "synced": counts.synced, "copied": counts.copied, "skipped": counts.skipped,
            "total": total, "size_mb": round(counts.total_size / (1024 * 1024), 1), "playlists": playlists,
            "errors": errors, **cleanup.event_fields()}


async def _copy_tracks(session: AsyncSession, target: Path, tracks: list[Track], skip: dict[int, str],
                       run: _SyncRun, publish) -> None:
    total = len(tracks)
    for i, track in enumerate(tracks):
        src = Path(track.file_path)
        if track.id in skip:
            run.errors.append(f"{track.title}: {skip[track.id]}")
        elif not src.exists():
            run.errors.append(f"Source missing: {track.title}")
        else:
            try:
                written, card_rows = await asyncio.to_thread(_sync_track, src, target, track, run.card_id, run.known)
                run.counts.add(written)
                run.copied.append(track)
                run.card_rows.extend(card_rows)
            except Exception as e:
                run.errors.append(f"{track.title}: {e}")
        publish({"type": "progress", "current": i + 1, "total": total, "synced": run.counts.synced,
                 "track": track.title, "artist": track.artist})
        # Commit in batches so an interrupted run still knows what it copied
        if (i + 1) % _SYNC_COMMIT_EVERY == 0:
            await _flush(session, run)
    await _flush(session, run)


async def _sync_library_to_device(session: AsyncSession, target: Path, scope: str, publish) -> None:
    tracks = await _load_sync_tracks(session, scope)
    total = len(tracks)
    if total == 0:
        publish(_done_event(_SyncCounts(), 0, 0, [], _CleanupResult()))
        return

    plan = await asyncio.to_thread(_plan_space, target, tracks)
    if not plan.fits:
        publish({"type": "error", "message": f"Not enough space on the card: this sync needs "
                                             f"{_size_label(plan.needed_bytes)} and {_size_label(plan.free_bytes)} "
                                             "is free. Free up space on the card or sync fewer tracks."})
        return
    try:
        card_id = await asyncio.to_thread(_card_identity, target)
    except OSError as e:
        logger.warning("Could not write %s on %s: %s", CARD_ID_FILE, target, e)
        publish({"type": "error", "message": "Leasure could not write to the card. Check that it is mounted "
                                             "and not write-protected, then sync again."})
        return

    publish({"type": "start", "total": total})
    run = _SyncRun(card_id=card_id, known=await _known_card_copies(session, card_id))
    playlists_generated = 0
    cleanup = _CleanupResult()
    try:
        await _copy_tracks(session, target, tracks, plan.skip, run, publish)
        publish({"type": "playlists", "message": "Generating playlists..."})
        playlists_generated = await _write_playlists(session, target, run.errors, card_id)
        cleanup = await _remove_superseded(target, card_id, session)
    except Exception as e:
        # Anything outside the per-track try (DB, playlist step, cleanup)
        logger.exception("Sync to %s aborted", target)
        run.errors.append(f"Sync aborted: {e}")
    finally:
        # Whatever happened, keep what was copied and record the run.
        await _record_sync(session, target, run, len(cleanup.removed_paths))

    publish(_done_event(run.counts, total, playlists_generated, run.errors, cleanup))


async def _run_sync(job: dict) -> None:
    """The sync itself, detached from the SSE response: a browser that navigates away
    only stops watching — the copy, the synced_at commits and the SyncHistory row
    all still happen. Progress goes to the job queue for whoever is streaming."""
    publish = job["queue"].put_nowait
    target = Path(job["device_path"])
    try:
        if not _is_accessible(target):
            publish({"type": "error", "message": f"Drive {target} not accessible. Make sure it is mounted."})
            return
        if not await asyncio.to_thread(_is_writable, target):
            publish({"type": "error", "message": "The card is write-protected (read-only). Check the lock switch on "
                                                 "the SD card or its adapter, then reinsert or remount it."})
            return
        async with async_session() as session:
            await _sync_library_to_device(session, target, job["scope"], publish)
    except Exception:
        logger.exception("Sync job for %s failed", target)
        publish({"type": "error", "message": "Sync failed. Check the server log."})
    finally:
        publish(None)


@router.post("/sync/start")
async def sync_start(device_path: str = Form(...), scope: str = Form("all")):
    """Validate the target and start the sync. Returns the job id to stream."""
    _prune_jobs()
    resolved = await asyncio.to_thread(resolve_sync_target, device_path)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail=f"{device_path} is not a detected removable drive. Click Scan and pick the H2.",
        )
    if resolved in _EJECTING:
        raise HTTPException(status_code=409, detail="The card is being ejected. Mount it again, then sync.")
    if scope not in ("all", "new"):
        raise HTTPException(status_code=400, detail="scope must be 'all' or 'new'")
    job_id = secrets.token_urlsafe(16)
    job = {"device_path": resolved, "scope": scope, "created": time.monotonic(),
           "queue": asyncio.Queue(), "streamed": False}
    job["task"] = asyncio.create_task(_run_sync(job), name=f"sync-{job_id}")
    _SYNC_JOBS[job_id] = job
    return {"job_id": job_id, "device_path": resolved, "scope": scope}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.get("/sync/stream/{job_id}")
async def sync_stream(job_id: str):
    """SSE progress for the job started by POST /sync/start. Watching is optional and
    single-use: the stream only drains the job's event queue."""
    _prune_jobs()
    job = _SYNC_JOBS.get(job_id)
    if not job or job["streamed"]:
        raise HTTPException(status_code=404, detail="Unknown or expired sync job. Start the sync again.")
    job["streamed"] = True
    queue = job["queue"]

    async def event_stream():
        while True:
            payload = await queue.get()
            if payload is None:
                return
            yield _sse(payload)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Read-only views of the card
# ---------------------------------------------------------------------------

async def _resolve_readable_target(device_path: str) -> Path | None:
    """The card as a Path, or None if `device_path` is not a detected removable
    drive or is not mounted. Read-only views trust the request no more than sync does."""
    resolved = await asyncio.to_thread(resolve_sync_target, device_path)
    if not resolved:
        return None
    target = Path(resolved)
    return target if _is_accessible(target) else None


def _scan_device_audio(target: Path) -> set[str]:
    """Relative POSIX paths of every audio file on the card (thread-side)."""
    found = set()
    for f in target.rglob("*"):
        if f.suffix.lower() in DEVICE_AUDIO_EXTS:
            # as_posix() so the comparison works on native Windows too, where
            # relative_to() would produce backslashes.
            found.add(f.relative_to(target).as_posix())
    return found


def _outdated_on_card(target: Path, pairs: list[tuple[Path, str]]) -> set[str]:
    """Card paths whose copy differs from the library file, i.e. the ones sync would copy again (thread-side)."""
    return {rel for src, rel in pairs if src.exists() and not _is_current(src, target / rel)}


def _judge_all(target: Path, records: list[_SupersededCopy], live: set[str]) -> list[str]:
    return [_judge_superseded(target, rec, live) for rec in records]


async def _superseded_preview(target: Path, session: AsyncSession) -> tuple[list[dict], list[dict]]:
    """What the next sync's cleanup would remove ("ready", or "waiting" for the replacement copy)
    and which old copies it would keep. Reads the card id without creating it."""
    card_id = await asyncio.to_thread(_read_card_id, target)
    records, live = await _load_superseded(session, card_id)
    if not records:
        return [], []
    verdicts = await asyncio.to_thread(_judge_all, target, records, live)
    to_remove: dict[str, dict] = {}
    kept: dict[str, dict] = {}
    for rec, verdict in zip(records, verdicts, strict=True):
        row = {"path": rec.rel_path, "title": rec.title, "artist": rec.artist}
        key = rec.rel_path.casefold()
        if verdict in ("ready", "waiting") and (key not in to_remove or verdict == "ready"):
            to_remove[key] = {**row, "state": verdict}
        elif verdict in ("path_in_use", "changed_on_card"):
            kept.setdefault(key, {**row, "reason": verdict})
    return list(to_remove.values()), [row for key, row in kept.items() if key not in to_remove]


@router.get("/diff/html")
async def sync_diff_html(
    request: Request,
    device_path: str,
    session: AsyncSession = Depends(get_session),
):
    target = await _resolve_readable_target(device_path)
    if target is None:
        return templates.TemplateResponse(request=request, name="partials/sync_diff.html",
                                          context={"error": f"Drive {device_path} not found"})

    device_files = await asyncio.to_thread(_scan_device_audio, target)
    tracks = await _load_sync_tracks(session, "all")

    to_add = []
    already_synced = []
    on_card = []

    for track in tracks:
        src = Path(track.file_path)
        rel_path = _device_rel_path(track, src.suffix.lstrip("."))
        if rel_path in device_files:
            on_card.append((track, src, rel_path))
            device_files.discard(rel_path)
        else:
            to_add.append({"title": track.title, "artist": track.artist, "album": track.album, "format": track.format})

    outdated = await asyncio.to_thread(_outdated_on_card, target, [(src, rel) for _, src, rel in on_card])
    for track, _, rel_path in on_card:
        row = {"title": track.title, "artist": track.artist, "album": track.album}
        if rel_path in outdated:
            # Sync re-copies these, so the diff counts them as to add.
            to_add.append({**row, "format": track.format, "changed": True})
        else:
            already_synced.append(row)

    # Remaining device_files are on device but not in library
    on_device_only = sorted(device_files)
    space = await asyncio.to_thread(_plan_space, target, tracks)
    to_remove, kept = await _superseded_preview(target, session)

    return templates.TemplateResponse(request=request, name="partials/sync_diff.html",
                                      context={
                                          "to_add": to_add,
                                          "already_synced": already_synced,
                                          "on_device_only": on_device_only,
                                          "space": space.context(),
                                          "to_remove": to_remove,
                                          "kept": kept,
                                      })


@router.get("/info")
async def device_info(device_path: str):
    target = await _resolve_readable_target(device_path)
    if target is None:
        return {"error": f"Device path {device_path} not found"}

    usage = await asyncio.to_thread(shutil.disk_usage, str(target))
    return {
        "path": device_path,
        "total_gb": round(usage.total / (1024**3), 1),
        "free_gb": round(usage.free / (1024**3), 1),
    }


def _scan_device_tree(target: Path) -> tuple[dict[str, list], int, list[str]]:
    """Artist → albums → files listing of the card (thread-side)."""
    artists: dict[str, list] = {}
    total_tracks = 0

    playlists = sorted(
        f.stem for f in target.iterdir()
        if f.is_file() and f.suffix.lower() in (".m3u", ".m3u8")
    )

    for artist_dir in sorted(target.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name in SYSTEM_FOLDER_NAMES:
            continue

        artist_albums = []
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                # Audio file directly under artist folder (no album subfolder)
                if album_dir.suffix.lower() in DEVICE_AUDIO_EXTS:
                    artist_albums.append({
                        "name": "(loose files)",
                        "tracks": 1,
                        "files": [{"name": album_dir.stem, "ext": album_dir.suffix,
                                   "size_mb": round(album_dir.stat().st_size / (1024 * 1024), 1)}],
                    })
                    total_tracks += 1
                continue

            tracks = []
            for f in sorted(album_dir.iterdir()):
                if f.suffix.lower() in DEVICE_AUDIO_EXTS:
                    tracks.append({
                        "name": f.stem,
                        "ext": f.suffix.lstrip(".").upper(),
                        "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                    })
            if tracks:
                artist_albums.append({"name": album_dir.name, "tracks": len(tracks), "files": tracks})
                total_tracks += len(tracks)

        if artist_albums:
            artists[artist_dir.name] = artist_albums

    return artists, total_tracks, playlists


@router.get("/files/html")
async def device_files_html(request: Request, device_path: str):
    target = await _resolve_readable_target(device_path)
    if target is None:
        return templates.TemplateResponse(request=request, name="partials/device_files.html",
                                          context={"error": f"Drive {device_path} not found"})

    artists, total_tracks, playlists = await asyncio.to_thread(_scan_device_tree, target)
    return templates.TemplateResponse(request=request, name="partials/device_files.html",
                                      context={"artists": artists, "total_tracks": total_tracks, "playlists": playlists})
