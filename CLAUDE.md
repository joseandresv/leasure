# Leasure -- Claude Code Context

## Project Overview

Leasure is a local music downloader and library manager designed specifically for the HIFI WALKER H2 portable music player. It runs as a FastAPI web server on native Windows, plain Linux, or WSL2 (see the platform layer below), provides a browser-based UI for browsing Spotify and YouTube Music libraries, downloads audio via yt-dlp, applies H2-compatible metadata, and syncs to the H2's SD card.

The target user has Spotify Premium and YouTube Music Premium subscriptions. The app uses these accounts for library browsing and leverages YouTube Music Premium for higher quality audio via Chrome cookie extraction.

## Key Architecture Decisions

### Platform layer (Windows / Linux / WSL2)
`services/platform.py` detects the runtime platform (`get_platform()` returns `windows` | `wsl2` | `linux`). Device detection in `services/device.py` dispatches to one of three backends: Win32 drive enumeration via ctypes (Windows), `/media` + `/run/media` mount scanning (Linux), or `/mnt/<letter>` drvfs scanning (WSL2). The manual `/api/device/mount` endpoint and its UI only apply on WSL2 — Windows and desktop Linux auto-mount removable drives. Templates receive `platform` and `device_path_placeholder` as Jinja globals. The cookie-extraction browser is configurable via `COOKIE_BROWSER` (default chrome) because Chrome 127+ app-bound encryption can block yt-dlp cookie extraction on native Windows (Firefox works there). Install scripts live in `scripts/` (install.sh/run.sh for Linux/WSL2, install.ps1/run.ps1 for Windows).

### yt-dlp instead of spotDL, in one engine
SpotDL was the original download engine but caused compatibility issues (dependency conflicts with the main app's Python environment, unreliable matching). The project now uses yt-dlp directly with ytmusicapi for search. `services/yt_engine.py` is the single pipeline for both Spotify-sourced and YouTube-sourced tracks (the old `spotdl_engine.py` / `ytdlp_engine.py` pair, which duplicated it, is gone). It resolves the source (an exact `youtube_id` wins over any search; otherwise scored ytmusicapi results, then scored `ytsearch5` results, with variant penalties and a minimum score -- no blind `ytsearch1`), downloads into `downloads/<track_id>/` via `extract_info(download=True)`, reads the provenance from `info["requested_downloads"][0]` (never by re-probing the output), verifies the duration within 10 s, then moves the file atomically into the library and removes the staging dir. There is no "most recent file in the folder" fallback. `Track.quality` stores the result tier (`aac_256`, `mp3_320`, ...) next to `source_format_id`, `source_codec`, `source_bitrate_kbps`, `source_sample_rate`, `premium_used`, `transcoded`, `is_lossless`, `verification`, `matched_title` and `match_score`. Lossy FLAC is no longer offered anywhere: `flac_lossless` fails with a message when no lossless engine is configured. Authenticated attempts are throttled and capped by `YT_MAX_DOWNLOADS_PER_DAY` (counter in `data/yt_daily.json`); a "Sign in to confirm" / 403 refusal stops the download and asks for fresh cookies instead of retrying cookie-less.

### Chrome cookie auto-refresh for YouTube Music
YouTube Music blocks unauthenticated and stale-cookie requests aggressively. The `youtube_client.py` has `_refresh_from_chrome()` which uses yt-dlp's cookie extraction to pull fresh cookies from Chrome, generates SAPISIDHASH auth, and writes ytmusicapi headers. This runs automatically on connection checks.

### Premium cookie gate
`data/cookies.txt` (Netscape format) is the credential yt-dlp authenticates with, not a browser profile: WSL2 has no readable profile and Chrome 127+ app-bound encryption blocks extraction on native Windows. `services/cookies.py` writes it from the `cookie` header of the manual header-paste flow (allow-listed Google/YouTube session cookies only, 0600, atomic), tracks its age (stale after 12 h) and source in `cookies.txt.meta.json`, and `premium_check()` proves the session really gets Premium audio by asking yt-dlp for the formats of `PREMIUM_CHECK_VIDEO_ID` on a `music.youtube.com` URL and looking for itag 141/774 or a "Premium" format note. The verdict is cached in `data/premium_check.json`, shown as PASS/FAIL on the YouTube Music card (`GET/POST /api/youtube/premium-check…`) and available as `python -m scripts.premium_check` (exit 1 on FAIL). Nothing downstream should claim Premium quality unless this passes.

### Deno for yt-dlp PO tokens
YouTube requires Proof of Origin tokens for some downloads. yt-dlp uses `remote_components: ["ejs:github"]` which requires deno to be installed. The `app.py` startup adds `~/.deno/bin` to PATH.

### MusicBrainz for genres instead of Spotify
Spotify's album genre endpoint (`/v1/albums/{id}`) almost always returns an empty array. The artist genre endpoint works but returns broad genres. MusicBrainz provides better genre data via community tags, so the tagger service falls back to MusicBrainz when Spotify genres are unavailable.

### No MUSIC/ prefix on SD card
The H2 scans the entire SD card for audio files and builds its Category browser from ID3 tags, not folder paths. Artist folders go directly at the SD card root. The `build_device_path()` function in `services/device.py` implements this.

### SQLite with async SQLAlchemy
Chose SQLite for simplicity (single-user app). Uses `aiosqlite` for async compatibility with FastAPI. The database lives at `data/leasure.db`.

### htmx for UI interactivity
The frontend uses htmx for partial page updates. Routers have both JSON API endpoints and `/html` endpoints that return Jinja2-rendered HTML partials. The SSE sync progress stream (`/api/device/sync/stream/{job_id}`) uses `StreamingResponse`.

### Sidecar files for H2
The H2 reads `.jpg` sidecar files (same name as audio) for album art and `.lrc` files for synced lyrics. These are created alongside the audio files in the library and copied during sync.

### Sync is a POST-created job streamed by id
`POST /api/device/sync/start` validates the target with `resolve_sync_target()` (must be a volume `detect_devices()` found, never `device_type == "system"`) and returns a single-use job id; `GET /api/device/sync/stream/{job_id}` streams SSE progress. The old `GET /sync/stream?device_path=` was CSRF-able (a drive-by page could copy the library anywhere and delete playlists) and is gone. `SameOriginMiddleware` in `app.py` additionally rejects mutating requests the browser marks cross-site.

### Playlist manifest on the card
`services/playlist.py` writes `.leasure-playlists.json` at the card root listing the `.m3u8` files Leasure generated. `sweep_orphan_playlists()` only deletes names from that manifest, so user-made playlists are never removed.

### One genre per file, full list in the DB
`Track.genre` keeps the comma-joined list (used by the genre map); `tagger.primary_genre()` writes only the first genre to TCON / `genre` / `\xa9gen` because the H2 groups by exact tag text. Native M4A downloads are tagged via `mutagen.mp4`; Opus via Vorbis comments.

### OAuth state
`services/oauth_state.py` issues one-shot state tokens (10 min TTL) for both the Spotify and Google flows; callbacks reject unknown state and handle `error=` (declined consent) with a redirect instead of a 422.

### Schema migration
`db.init_db()` runs `create_all` and then `_migrate_missing_columns()`, which adds any column the models declare but the table lacks (additive only) after copying `leasure.db` to a timestamped `.bak`. SQLite pragmas (`foreign_keys`, WAL, `busy_timeout`) are set per connection.

## File Structure

```
app.py              -- FastAPI app entry point, lifespan, page routes
config.py           -- Pydantic Settings (reads .env)
db.py               -- SQLAlchemy async engine, session factory
models.py           -- ORM models: Track, Playlist, PlaylistTrack, SyncHistory
worker.py           -- Async download queue (asyncio.Queue + worker tasks)

routers/
  spotify.py        -- Spotify browse + download (htmx + JSON endpoints)
  youtube.py        -- YouTube Music browse + download
  device.py         -- Device detection, sync (SSE stream), diff, file browser
  downloads.py      -- Download queue status
  library.py        -- Library browsing

services/
  spotify_client.py -- Spotipy OAuth wrapper
  youtube_client.py -- ytmusicapi wrapper with Chrome cookie auto-refresh
  downloader.py     -- Download dispatcher (yt_engine, lossless engines)
  yt_engine.py      -- The YouTube pipeline: scored match, staging dir, provenance, atomic move
  ytdlp_opts.py     -- Shared yt-dlp option sets (cookie file, throttle, PO-token solver)
  streamrip_engine.py -- Qobuz/Tidal/Deezer lossless (optional, requires credentials)
  bandcamp_engine.py  -- Bandcamp lossless fallback (optional)
  archive_engine.py   -- Internet Archive lossless fallback (optional)
  tagger.py         -- Full metadata pipeline: ID3/Vorbis tags, embedded art, sidecar jpg/lrc
  lyrics.py         -- lrclib.net synced lyrics fetcher
  artwork.py        -- Album art download + Pillow resize
  platform.py       -- Runtime platform detection (windows | wsl2 | linux)
  formats.py        -- Shared download format/quality mapping
  device.py         -- Device detection (per-platform backends), FAT32 filename sanitization, device path builder
  playlist.py       -- M3U playlist generation (H2 format) + on-card manifest of generated files
  oauth_state.py    -- one-shot OAuth state tokens shared by Spotify and Google flows

tests/              -- pytest suite (ASGI client, temp data dir); .github/workflows/ci.yml runs ruff + pytest
.claude/agents/     -- frontend-dev, backend-dev (Opus, load coding-standards), user-tester (Opus, read-only, loads ui-testing)
.claude/skills/     -- coding-standards (rules for change agents), ui-testing (Playwright headless-browser QA recipe + probe.py),
                       language skills: python-craft, jinja-htmx, javascript-craft, css-craft, shell-scripts;
                       frontend-design + webapp-testing (Anthropic's official skills, Apache-2.0, vendored)
docs/review-2026-08 -- Aug 2026 review output (gitignored; see its README.md and STATUS.md)

templates/          -- Jinja2 templates (base.html + page templates + htmx partials)
static/             -- CSS, JS assets
data/               -- SQLite DB, Spotify cache, YouTube headers (gitignored)
library/            -- Downloaded music (gitignored)
downloads/          -- Temp staging (gitignored)
```

## Common Issues and Fixes

### spotDL incompatibility
SpotDL has heavy dependencies (bandcamp-dl, beautifulsoup4, etc.) that conflict with this project's dependency tree. It also requires its own venv. The solution was to replace spotDL entirely with direct yt-dlp + ytmusicapi. If spotDL is ever needed again, use a separate `.venv-spotdl/` and subprocess calls.

### YouTube Music blocking / 403 errors
YouTube Music aggressively blocks requests with stale or missing cookies. Symptoms: yt-dlp fails with 403, ytmusicapi returns empty results. Fix: ensure Chrome is logged into music.youtube.com, then the auto-refresh in `youtube_client.py` will extract fresh cookies. If auto-refresh fails, use the manual header paste flow in the YouTube Music UI.

### yt-dlp PO token errors
If yt-dlp fails with "Sign in to confirm you're not a bot", deno is likely not installed or not on PATH. Install deno (`curl -fsSL https://deno.land/install.sh | sh`) and ensure `~/.deno/bin` is in PATH. The `app.py` startup handles this, but it needs to exist.

### FAT32 / drvfs permission issues on WSL2
Windows drives mounted via WSL2 (drvfs/9p) have permission quirks. `shutil.copyfile` works but `shutil.copy2` may fail (can't set permissions on drvfs). Filenames must be FAT32-safe (no `\/:*?"<>|`). The `sanitize_filename()` function in `services/device.py` handles this.

### Spotify API genre endpoint returning empty
`/v1/albums/{id}` almost always returns `"genres": []`. The workaround is to fetch genres from the artist endpoint instead, but even that returns broad categories. The tagger falls back to MusicBrainz which has community-curated genre tags. See `_fetch_genre()` in `services/tagger.py`.

### Cookie / token expiry
- Spotify OAuth tokens auto-refresh via spotipy's cache mechanism (`data/.spotify_cache`)
- YouTube Music cookies expire after a few hours. The `_refresh_from_chrome()` method in `youtube_client.py` re-extracts them from Chrome on each connection check
- If Chrome is not running or not logged in, YouTube features will fail silently

### Album artist defaulting
The H2 uses the album artist (TPE2/albumartist) tag for its Category browser. If album artist is not set, tracks appear under "Unknown Artist" in the H2's artist view even if TPE1 is correct. The tagger defaults TPE2 to the primary artist (first name before comma) when album artist is not explicitly provided.

## How to Test

### Automated
```bash
ruff check .
pytest -q          # tests/ — temp data dir, ASGI client, no network; fixtures in tests/conftest.py
```
`tests/conftest.py` sets `DATA_DIR`/`LIBRARY_DIR`/`DOWNLOAD_DIR` env vars before importing `config`, and the `fake_device` fixture monkeypatches `detect_devices()` so sync tests get a temp "H2".

### Browser-level QA
The `user-tester` agent drives the running app in headless Chromium via the `ui-testing` skill (`.claude/skills/ui-testing/`). Python Playwright lives in `~/.venvs/leasure-tester`; Chromium needs `LD_LIBRARY_PATH=~/.local/chromium-deps/usr/lib/x86_64-linux-gnu` on this box (libs extracted from .deb without root).

### Run the development server
```bash
source .venv/bin/activate          # or: LEASURE_VENV=~/.venvs/leasure scripts/run.sh
uvicorn app:app --host 127.0.0.1 --port 8642 --reload
```

### Test with ASGI transport (no server needed)
```python
import pytest
from httpx import ASGITransport, AsyncClient
from app import app

@pytest.mark.asyncio
async def test_home():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
```

### Manual test checklist
1. Start server, open http://127.0.0.1:8642
2. Verify status bar loads (htmx polling)
3. Connect Spotify via OAuth flow
4. Browse albums/playlists, download a track
5. Check library page shows the downloaded track
6. Connect H2 via USB, go to Device page
7. Verify device detection finds the correct drive
8. Preview sync diff, then sync with progress

## Environment

- Runs on native Windows, plain Linux, or WSL2 (primary dev environment: WSL2 Ubuntu with Windows drives at /mnt/)
- Python 3.11+ with venv at `.venv/` (created by `scripts/install.sh` / `scripts/install.ps1`); on this WSL2 checkout the venv lives at `~/.venvs/leasure` (OneDrive folder) — start with `LEASURE_VENV=~/.venvs/leasure scripts/run.sh`
- ffmpeg and deno are installed user-side here (`~/.local/bin/ffmpeg`, `~/.deno/bin/deno`) because sudo needs a password
- ffmpeg must be installed (`sudo apt install ffmpeg` / `winget install Gyan.FFmpeg`)
- deno must be installed for yt-dlp PO tokens
- A browser logged into YouTube Music for Premium quality (Chrome by default; set `COOKIE_BROWSER=firefox` on native Windows if Chrome cookie extraction is blocked)
- WARNING: keeping the repo inside a OneDrive-synced folder causes intermittent EIO through the WSL drvfs bridge when OneDrive dehydrates files; pin the folder ("Always keep on this device") or move it out of OneDrive
