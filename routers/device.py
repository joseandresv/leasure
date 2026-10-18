import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session, get_session
from models import Playlist, PlaylistTrack, SyncHistory, Track, utc_now
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
    """Copy unless the card copy is current; True when bytes were written."""
    if _is_current(src, dst):
        return False
    # copyfile, not copy2: drvfs/FAT32 cannot take the permission bits copy2 sets
    shutil.copyfile(str(src), str(dst))
    src_stat = src.stat()
    try:
        # The source mtime on the copy is what lets the next sync recognise it as current.
        os.utime(dst, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
    except OSError as e:
        logger.debug("Could not set the mtime of %s: %s", dst, e)
    return True


def _copy_track(src: Path, target: Path, track: Track) -> int | None:
    """Copy the audio file plus its .jpg/.lrc sidecars, skipping files the card already
    holds unchanged. Runs in a worker thread. Returns the size of the audio file written,
    or None when the card copy was already current."""
    ext = src.suffix.lstrip(".")
    dst = target / _device_rel_path(track, ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    copied = _copy_file(src, dst)
    for sidecar_ext in ("jpg", "lrc"):
        sidecar_src = src.with_suffix(f".{sidecar_ext}")
        if sidecar_src.exists():
            _copy_file(sidecar_src, target / _device_rel_path(track, sidecar_ext))
    return dst.stat().st_size if copied else None


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

    def history(self, target: Path) -> SyncHistory:
        return SyncHistory(device_path=str(target), tracks_added=self.copied, tracks_skipped=self.skipped,
                           total_size=self.total_size)


async def _write_playlists(session: AsyncSession, target: Path, errors: list[str]) -> int:
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
                         "duration_ms": t.duration_ms}
                    )
            if pl_tracks:
                written.append(await asyncio.to_thread(generate_m3u, pl.name, pl_tracks, target))
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
    target = Path(resolved)
    _LEGACY_SYNC_TARGETS.append(resolved)
    counts = _SyncCounts()
    errors: list[str] = []
    copied: list[Track] = []
    try:
        tracks = await _load_sync_tracks(session, scope)
        for track in tracks:
            src = Path(track.file_path)
            if not src.exists():
                errors.append(f"Source file missing: {track.title}")
                continue
            try:
                counts.add(await asyncio.to_thread(_copy_track, src, target, track))
                copied.append(track)
            except Exception as e:
                errors.append(f"Failed to copy {track.title}: {e}")
        await _mark_synced(session, copied)
        await session.commit()
        playlists_generated = await _write_playlists(session, target, errors)
    finally:
        _LEGACY_SYNC_TARGETS.remove(resolved)
        await _mark_synced(session, copied)
        session.add(counts.history(target))
        await session.commit()

    return templates.TemplateResponse(request=request, name="partials/sync_result.html",
                                      context={
                                          "synced": counts.synced,
                                          "copied": counts.copied,
                                          "skipped": counts.skipped,
                                          "total_size_mb": round(counts.total_size / (1024 * 1024), 1),
                                          "playlists_generated": playlists_generated,
                                          "errors": errors,
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


async def _record_sync(session: AsyncSession, target: Path, counts: _SyncCounts, copied: list[Track]) -> None:
    try:
        await _mark_synced(session, copied)
        await session.commit()
        session.add(counts.history(target))
        await session.commit()
    except Exception:
        logger.exception("Failed to record sync history")


async def _sync_library_to_device(session: AsyncSession, target: Path, scope: str, publish) -> None:
    tracks = await _load_sync_tracks(session, scope)
    total = len(tracks)
    if total == 0:
        publish({"type": "done", "synced": 0, "copied": 0, "skipped": 0, "total": 0, "size_mb": 0,
                 "playlists": 0, "errors": []})
        return

    publish({"type": "start", "total": total})

    counts = _SyncCounts()
    errors: list[str] = []
    copied: list[Track] = []
    playlists_generated = 0
    try:
        for i, track in enumerate(tracks):
            src = Path(track.file_path)
            if not src.exists():
                errors.append(f"Source missing: {track.title}")
            else:
                try:
                    counts.add(await asyncio.to_thread(_copy_track, src, target, track))
                    copied.append(track)
                except Exception as e:
                    errors.append(f"{track.title}: {e}")
            publish({"type": "progress", "current": i + 1, "total": total, "synced": counts.synced,
                     "track": track.title, "artist": track.artist})
            # Commit in batches so an interrupted run still knows what it copied
            if (i + 1) % _SYNC_COMMIT_EVERY == 0:
                await _mark_synced(session, copied)
                await session.commit()

        await _mark_synced(session, copied)
        await session.commit()

        publish({"type": "playlists", "message": "Generating playlists..."})
        playlists_generated = await _write_playlists(session, target, errors)
    except Exception as e:
        # Anything outside the per-track try (DB, playlist step)
        logger.exception("Sync to %s aborted", target)
        errors.append(f"Sync aborted: {e}")
    finally:
        # Whatever happened, keep what was copied and record the run.
        await _record_sync(session, target, counts, copied)

    publish({"type": "done", "synced": counts.synced, "copied": counts.copied, "skipped": counts.skipped,
             "total": total, "size_mb": round(counts.total_size / (1024 * 1024), 1),
             "playlists": playlists_generated, "errors": errors})


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

    stmt = select(Track).where(Track.status == "done")
    result = await session.execute(stmt)
    tracks = result.scalars().all()

    to_add = []
    already_synced = []
    on_card = []

    for track in tracks:
        if not track.file_path:
            continue
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

    return templates.TemplateResponse(request=request, name="partials/sync_diff.html",
                                      context={
                                          "to_add": to_add,
                                          "already_synced": already_synced,
                                          "on_device_only": on_device_only,
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
