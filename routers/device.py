import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import get_session
from models import Playlist, PlaylistTrack, SyncHistory, Track
from services.device import build_device_path, detect_devices, sanitize_filename
from services.playlist import generate_m3u

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _is_accessible(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


@router.post("/mount")
async def mount_drive(letter: str = Form(...)):
    """Mount a Windows drive letter in WSL2 via drvfs."""
    import re
    import subprocess

    # Validate: single letter a-z
    letter = letter.strip().lower()
    if not re.match(r'^[a-z]$', letter):
        return HTMLResponse(f'<p style="color:var(--color-error);">Invalid drive letter: {letter}</p>')

    mount_path = f"/mnt/{letter}"

    # Check if already mounted
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_path:
                    return HTMLResponse(f'<p style="color:var(--color-success);">{letter.upper()}: already mounted at {mount_path}</p>')
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
        result = subprocess.run(
            ["sudo", "mount", "-t", "drvfs", drive_spec, mount_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return HTMLResponse(
                f'<p style="color:var(--color-success);">{letter.upper()}: mounted at {mount_path}</p>'
                f'<script>setTimeout(function(){{ htmx.ajax("GET","/api/device/detect/html","#device-list") }}, 500)</script>'
            )
        else:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            # Common: needs passwordless sudo for mount
            if "password" in err.lower() or "sudo" in err.lower():
                return HTMLResponse(
                    f'<p style="color:var(--color-error);">Sudo password required. Run this in your terminal:</p>'
                    f'<code style="display:block;margin-top:0.3rem;font-size:0.85em;">sudo mount -t drvfs {drive_spec} {mount_path}</code>'
                )
            return HTMLResponse(f'<p style="color:var(--color-error);">Mount failed: {err[:200]}</p>')
    except subprocess.TimeoutExpired:
        return HTMLResponse(f'<p style="color:var(--color-error);">Mount timed out — drive may not be connected</p>')
    except Exception as e:
        return HTMLResponse(f'<p style="color:var(--color-error);">Error: {str(e)[:200]}</p>')


@router.get("/detect")
async def detect():
    return detect_devices()


@router.get("/detect/html")
async def detect_html(request: Request):
    devices = detect_devices()
    return templates.TemplateResponse(request=request, name="partials/device_list.html",
                                      context={"devices": devices})


@router.post("/sync")
async def sync_to_device(
    request: Request,
    device_path: str = Form(...),
    scope: str = Form("all"),
    session: AsyncSession = Depends(get_session),
):
    target = Path(device_path)
    if not _is_accessible(target):
        return templates.TemplateResponse(request=request, name="partials/sync_result.html",
                                          context={"error": f"Drive {device_path} not found. Make sure it's plugged in and mounted."})

    stmt = select(Track).where(Track.status == "done")
    if scope == "new":
        stmt = stmt.where(Track.synced_at.is_(None))
    result = await session.execute(stmt)
    tracks = result.scalars().all()

    synced = 0
    total_size = 0
    errors = []

    for track in tracks:
        if not track.file_path:
            continue
        src = Path(track.file_path)
        if not src.exists():
            errors.append(f"Source file missing: {track.title}")
            continue

        ext = src.suffix.lstrip(".")
        rel_path = build_device_path(
            track.artist, track.album, track.track_number, track.title, ext
        )
        dst = target / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copyfile(str(src), str(dst))
            total_size += dst.stat().st_size
            synced += 1

            for sidecar_ext in ["jpg", "lrc"]:
                sidecar_src = src.with_suffix(f".{sidecar_ext}")
                if sidecar_src.exists():
                    sidecar_rel = build_device_path(
                        track.artist, track.album, track.track_number, track.title, sidecar_ext
                    )
                    sidecar_dst = target / sidecar_rel
                    shutil.copyfile(str(sidecar_src), str(sidecar_dst))

            track.synced_at = datetime.utcnow()
        except Exception as e:
            errors.append(f"Failed to copy {track.title}: {e}")

    await session.commit()

    # Generate playlists (.m3u8 at SD card root)
    playlists_generated = 0
    try:
        all_synced = await session.execute(
            select(Track).where(Track.status == "done", Track.file_path.isnot(None))
        )
        all_tracks = all_synced.scalars().all()

        if all_tracks:
            # Only generate playlists from user-created playlists (Spotify/YouTube)
            # Albums are already organized by ID3 tags on the H2
            playlist_result = await session.execute(select(Playlist))
            db_playlists = playlist_result.scalars().all()
            for pl in db_playlists:
                entries = await session.execute(
                    select(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id).order_by(PlaylistTrack.position)
                )
                track_ids = [e.track_id for e in entries.scalars().all()]
                pl_tracks = []
                for tid in track_ids:
                    t = await session.get(Track, tid)
                    if t and t.status == "done" and t.file_path:
                        pl_tracks.append(
                            {"artist": t.artist, "album": t.album, "track_number": t.track_number,
                             "title": t.title, "format": t.format, "duration_ms": t.duration_ms}
                        )
                if pl_tracks:
                    generate_m3u(pl.name, pl_tracks, target)
                    playlists_generated += 1

    except Exception as e:
        errors.append(f"Playlist generation error: {e}")
        logger.exception("Failed to generate playlists")

    history = SyncHistory(
        device_path=device_path,
        tracks_added=synced,
        total_size=total_size,
    )
    session.add(history)
    await session.commit()

    return templates.TemplateResponse(request=request, name="partials/sync_result.html",
                                      context={
                                          "synced": synced,
                                          "total_size_mb": round(total_size / (1024 * 1024), 1),
                                          "playlists_generated": playlists_generated,
                                          "errors": errors,
                                      })


@router.get("/sync/stream")
async def sync_stream(device_path: str, scope: str = "all"):
    """SSE endpoint for sync progress."""

    async def event_stream():
        target = Path(device_path)
        try:
            is_valid = target.is_dir()
        except OSError:
            is_valid = False
        if not is_valid:
            yield f"data: {json.dumps({'type': 'error', 'message': f'Drive {device_path} not accessible. Make sure it is mounted.'})}\n\n"
            return

        async with async_session() as session:
            stmt = select(Track).where(Track.status == "done")
            if scope == "new":
                stmt = stmt.where(Track.synced_at.is_(None))
            result = await session.execute(stmt)
            tracks = result.scalars().all()

            total = len([t for t in tracks if t.file_path])
            if total == 0:
                yield f"data: {json.dumps({'type': 'done', 'synced': 0, 'total': 0, 'size_mb': 0, 'playlists': 0, 'errors': []})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

            synced = 0
            total_size = 0
            errors = []

            for i, track in enumerate(tracks):
                if not track.file_path:
                    continue
                src = Path(track.file_path)
                if not src.exists():
                    errors.append(f"Source missing: {track.title}")
                    continue

                ext = src.suffix.lstrip(".")
                rel_path = build_device_path(track.artist, track.album, track.track_number, track.title, ext)
                dst = target / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)

                try:
                    await asyncio.to_thread(shutil.copyfile, str(src), str(dst))
                    total_size += dst.stat().st_size
                    synced += 1

                    for sidecar_ext in ["jpg", "lrc"]:
                        sidecar_src = src.with_suffix(f".{sidecar_ext}")
                        if sidecar_src.exists():
                            sidecar_rel = build_device_path(track.artist, track.album, track.track_number, track.title, sidecar_ext)
                            sidecar_dst = target / sidecar_rel
                            await asyncio.to_thread(shutil.copyfile, str(sidecar_src), str(sidecar_dst))

                    track.synced_at = datetime.utcnow()
                except Exception as e:
                    errors.append(f"{track.title}: {e}")

                yield f"data: {json.dumps({'type': 'progress', 'current': i + 1, 'total': total, 'synced': synced, 'track': track.title, 'artist': track.artist})}\n\n"

            await session.commit()

            # Generate playlists
            playlists_generated = 0
            try:
                yield f"data: {json.dumps({'type': 'playlists', 'message': 'Generating playlists...'})}\n\n"

                all_result = await session.execute(select(Track).where(Track.status == "done", Track.file_path.isnot(None)))
                all_tracks = all_result.scalars().all()

                if all_tracks:
                    # Only generate playlists from user-created playlists (Spotify/YouTube)
                    # Albums are already organized by ID3 tags on the H2
                    playlist_result = await session.execute(select(Playlist))
                    db_playlists = playlist_result.scalars().all()
                    for pl in db_playlists:
                        entries = await session.execute(
                            select(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id).order_by(PlaylistTrack.position)
                        )
                        track_ids = [e.track_id for e in entries.scalars().all()]
                        pl_tracks = []
                        for tid in track_ids:
                            t = await session.get(Track, tid)
                            if t and t.status == "done" and t.file_path:
                                pl_tracks.append(
                                    {"artist": t.artist, "album": t.album, "track_number": t.track_number,
                                     "title": t.title, "format": t.format, "duration_ms": t.duration_ms})
                        if pl_tracks:
                            generate_m3u(pl.name, pl_tracks, target)
                            playlists_generated += 1
            except Exception as e:
                errors.append(f"Playlist error: {e}")

            # Save history
            session.add(SyncHistory(device_path=device_path, tracks_added=synced, total_size=total_size))
            await session.commit()

            yield f"data: {json.dumps({'type': 'done', 'synced': synced, 'total': total, 'size_mb': round(total_size / (1024 * 1024), 1), 'playlists': playlists_generated, 'errors': errors})}\n\n"

    from db import async_session
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/diff/html")
async def sync_diff_html(
    request: Request,
    device_path: str,
    session: AsyncSession = Depends(get_session),
):
    target = Path(device_path)
    if not _is_accessible(target):
        return templates.TemplateResponse(request=request, name="partials/sync_diff.html",
                                          context={"error": f"Drive {device_path} not found"})

    AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ape", ".dsf", ".dff"}

    # Get all audio files on device
    device_files = set()
    for f in target.rglob("*"):
        if f.suffix.lower() in AUDIO_EXTS:
            device_files.add(str(f.relative_to(target)))

    # Get all library tracks
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
    target = Path(device_path)
    if not _is_accessible(target):
        return {"error": f"Device path {device_path} not found"}

    usage = shutil.disk_usage(str(target))
    return {
        "path": device_path,
        "total_gb": round(usage.total / (1024**3), 1),
        "free_gb": round(usage.free / (1024**3), 1),
    }


@router.get("/files/html")
async def device_files_html(request: Request, device_path: str):
    target = Path(device_path)
    if not _is_accessible(target):
        return templates.TemplateResponse(request=request, name="partials/device_files.html",
                                          context={"error": f"Drive {device_path} not found"})

    AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ape", ".dsf", ".dff", ".ogg", ".m4a", ".opus"}
    artists: dict[str, list] = {}
    total_tracks = 0

    # Find playlist files at root
    playlists = sorted(
        f.stem for f in target.iterdir()
        if f.is_file() and f.suffix.lower() in (".m3u", ".m3u8")
    )

    for artist_dir in sorted(target.iterdir()):
        if not artist_dir.is_dir():
            continue
        # Skip system folders
        if artist_dir.name in ("$RECYCLE.BIN", "System Volume Information", ".Trash-1000"):
            continue

        artist_albums = []
        for album_dir in sorted(artist_dir.iterdir()):
            if not album_dir.is_dir():
                # Audio file directly under artist folder (no album subfolder)
                if album_dir.suffix.lower() in AUDIO_EXTS:
                    artist_albums.append({
                        "name": "(loose files)",
                        "tracks": 1,
                        "files": [{"name": album_dir.stem, "ext": album_dir.suffix, "size_mb": round(album_dir.stat().st_size / (1024 * 1024), 1)}],
                    })
                    total_tracks += 1
                continue

            tracks = []
            for f in sorted(album_dir.iterdir()):
                if f.suffix.lower() in AUDIO_EXTS:
                    tracks.append({
                        "name": f.stem,
                        "ext": f.suffix.lstrip(".").upper(),
                        "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                    })
            if tracks:
                artist_albums.append({
                    "name": album_dir.name,
                    "tracks": len(tracks),
                    "files": tracks,
                })
                total_tracks += len(tracks)

        if artist_albums:
            artists[artist_dir.name] = artist_albums

    return templates.TemplateResponse(request=request, name="partials/device_files.html",
                                      context={"artists": artists, "total_tracks": total_tracks, "playlists": playlists})
