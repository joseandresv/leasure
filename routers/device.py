import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session, get_session
from models import Playlist, PlaylistTrack, SyncHistory, Track, utc_now
from services.device import build_device_path, detect_devices, resolve_sync_target
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


def _msg(text: str, color: str = "--text-secondary") -> HTMLResponse:
    return HTMLResponse(f'<p style="color:var({color});">{escape(text)}</p>')


@router.post("/mount")
async def mount_drive(letter: str = Form(...)):
    """Mount a Windows drive letter in WSL2 via drvfs. No-op elsewhere: the OS auto-mounts."""
    if get_platform() != "wsl2":
        return _msg("Removable drives mount automatically on this OS. Plug in the device and click Scan.")

    # Validate: single letter a-z
    letter = letter.strip().lower()
    if not re.match(r"^[a-z]$", letter):
        return _msg(f"Invalid drive letter: {letter}", "--color-error")

    mount_path = f"/mnt/{letter}"

    # Check if already mounted
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_path:
                    return _msg(f"{letter.upper()}: already mounted at {mount_path}", "--color-success")
    except OSError:
        pass

    # Create mount point if needed
    try:
        os.makedirs(mount_path, exist_ok=True)
    except OSError:
        pass

    # Try to mount via drvfs
    drive_spec = f"{letter.upper()}:"
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", "mount", "-t", "drvfs", drive_spec, mount_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return HTMLResponse(
                f'<p style="color:var(--color-success);">{escape(drive_spec)} mounted at {escape(mount_path)}</p>'
                '<script>setTimeout(function(){ htmx.ajax("GET","/api/device/detect/html","#device-list") }, 500)</script>'
            )
        err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        # Common: needs passwordless sudo for mount
        if "password" in err.lower() or "sudo" in err.lower():
            return HTMLResponse(
                '<p style="color:var(--text-secondary);">Sudo password required. Run this in your terminal:</p>'
                '<code style="display:block;margin-top:0.3rem;font-size:0.85em;user-select:all;">'
                f'sudo mount -t drvfs {escape(drive_spec)} {escape(mount_path)}</code>'
            )
        return _msg(f"Mount failed: {err[:200]}", "--color-error")
    except subprocess.TimeoutExpired:
        return _msg("Mount timed out — drive may not be connected", "--color-error")
    except Exception as e:
        return _msg(f"Error: {str(e)[:200]}", "--color-error")


@router.get("/detect")
async def detect():
    return await asyncio.to_thread(detect_devices)


@router.get("/detect/html")
async def detect_html(request: Request):
    devices = await asyncio.to_thread(detect_devices)
    return templates.TemplateResponse(request=request, name="partials/device_list.html",
                                      context={"devices": devices})


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

async def _load_sync_tracks(session: AsyncSession, scope: str) -> list[Track]:
    stmt = select(Track).where(Track.status == "done")
    if scope == "new":
        stmt = stmt.where(Track.synced_at.is_(None))
    result = await session.execute(stmt)
    return [t for t in result.scalars().all() if t.file_path]


def _copy_track(src: Path, target: Path, track: Track) -> int:
    """Copy the audio file plus its .jpg/.lrc sidecars. Runs in a worker thread.
    Returns the size of the audio file on the device."""
    ext = src.suffix.lstrip(".")
    dst = target / build_device_path(track.artist, track.album, track.track_number, track.title, ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # copyfile, not copy2: drvfs/FAT32 cannot take the permission bits copy2 sets
    shutil.copyfile(str(src), str(dst))
    size = dst.stat().st_size
    for sidecar_ext in ("jpg", "lrc"):
        sidecar_src = src.with_suffix(f".{sidecar_ext}")
        if sidecar_src.exists():
            sidecar_dst = target / build_device_path(
                track.artist, track.album, track.track_number, track.title, sidecar_ext
            )
            shutil.copyfile(str(sidecar_src), str(sidecar_dst))
    return size


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
                        {"artist": t.artist, "album": t.album, "track_number": t.track_number,
                         "title": t.title, "format": t.format, "duration_ms": t.duration_ms}
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
    if not resolved:
        return templates.TemplateResponse(
            request=request, name="partials/sync_result.html",
            context={"error": f"{device_path} is not a detected removable drive. Click Scan and pick the H2."},
        )
    target = Path(resolved)
    tracks = await _load_sync_tracks(session, scope)

    synced = 0
    total_size = 0
    errors: list[str] = []
    try:
        for track in tracks:
            src = Path(track.file_path)
            if not src.exists():
                errors.append(f"Source file missing: {track.title}")
                continue
            try:
                total_size += await asyncio.to_thread(_copy_track, src, target, track)
                synced += 1
                track.synced_at = utc_now()
            except Exception as e:
                errors.append(f"Failed to copy {track.title}: {e}")
        await session.commit()
        playlists_generated = await _write_playlists(session, target, errors)
    finally:
        session.add(SyncHistory(device_path=str(target), tracks_added=synced, total_size=total_size))
        await session.commit()

    return templates.TemplateResponse(request=request, name="partials/sync_result.html",
                                      context={
                                          "synced": synced,
                                          "total_size_mb": round(total_size / (1024 * 1024), 1),
                                          "playlists_generated": playlists_generated,
                                          "errors": errors,
                                      })


# A sync is created with POST (so a cross-site navigation or <img> cannot start one)
# and then observed with GET on its job id. Jobs are single-use and expire quickly.
_SYNC_JOBS: dict[str, dict] = {}
_SYNC_JOB_TTL = 300  # seconds
_SYNC_COMMIT_EVERY = 25


def _prune_jobs() -> None:
    """Drop expired jobs and the events they never delivered. A job whose task is
    still copying is kept whatever its age, so the task is never left unreferenced."""
    now = time.monotonic()
    for job_id, job in list(_SYNC_JOBS.items()):
        if job["task"].done() and now - job["created"] > _SYNC_JOB_TTL:
            _SYNC_JOBS.pop(job_id, None)


async def _record_sync(session: AsyncSession, target: Path, synced: int, total_size: int) -> None:
    try:
        await session.commit()
        session.add(SyncHistory(device_path=str(target), tracks_added=synced, total_size=total_size))
        await session.commit()
    except Exception:
        logger.exception("Failed to record sync history")


async def _sync_library_to_device(session: AsyncSession, target: Path, scope: str, publish) -> None:
    tracks = await _load_sync_tracks(session, scope)
    total = len(tracks)
    if total == 0:
        publish({"type": "done", "synced": 0, "total": 0, "size_mb": 0, "playlists": 0, "errors": []})
        return

    publish({"type": "start", "total": total})

    synced = 0
    total_size = 0
    errors: list[str] = []
    playlists_generated = 0
    try:
        for i, track in enumerate(tracks):
            src = Path(track.file_path)
            if not src.exists():
                errors.append(f"Source missing: {track.title}")
            else:
                try:
                    total_size += await asyncio.to_thread(_copy_track, src, target, track)
                    synced += 1
                    track.synced_at = utc_now()
                except Exception as e:
                    errors.append(f"{track.title}: {e}")
            publish({"type": "progress", "current": i + 1, "total": total, "synced": synced,
                     "track": track.title, "artist": track.artist})
            # Commit in batches so an interrupted run still knows what it copied
            if (i + 1) % _SYNC_COMMIT_EVERY == 0:
                await session.commit()

        await session.commit()

        publish({"type": "playlists", "message": "Generating playlists..."})
        playlists_generated = await _write_playlists(session, target, errors)
    except Exception as e:
        # Anything outside the per-track try (DB, playlist step)
        logger.exception("Sync to %s aborted", target)
        errors.append(f"Sync aborted: {e}")
    finally:
        # Whatever happened, keep what was copied and record the run.
        await _record_sync(session, target, synced, total_size)

    publish({"type": "done", "synced": synced, "total": total,
             "size_mb": round(total_size / (1024 * 1024), 1),
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

    for track in tracks:
        if not track.file_path:
            continue
        src = Path(track.file_path)
        ext = src.suffix.lstrip(".")
        rel_path = build_device_path(
            track.artist, track.album, track.track_number, track.title, ext
        )
        if rel_path in device_files:
            already_synced.append({"title": track.title, "artist": track.artist, "album": track.album})
            device_files.discard(rel_path)
        else:
            to_add.append({"title": track.title, "artist": track.artist, "album": track.album, "format": track.format})

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
