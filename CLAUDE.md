# Leasure -- Claude Code Context

## Project Overview

Leasure is a local music downloader and library manager designed specifically for the HIFI WALKER H2 portable music player. It runs as a FastAPI web server on WSL2, provides a browser-based UI for browsing Spotify and YouTube Music libraries, downloads audio via yt-dlp, applies H2-compatible metadata, and syncs to the H2's SD card.

The target user has Spotify Premium and YouTube Music Premium subscriptions. The app uses these accounts for library browsing and leverages YouTube Music Premium for higher quality audio via Chrome cookie extraction.

## Key Architecture Decisions

### yt-dlp instead of spotDL
SpotDL was the original download engine but caused compatibility issues (dependency conflicts with the main app's Python environment, unreliable matching). The project now uses yt-dlp directly with ytmusicapi for search. The `spotdl_engine.py` file name is historical -- it actually implements yt-dlp + ytmusicapi downloads, not spotDL.

### Chrome cookie auto-refresh for YouTube Music
YouTube Music blocks unauthenticated and stale-cookie requests aggressively. The `youtube_client.py` has `_refresh_from_chrome()` which uses yt-dlp's cookie extraction to pull fresh cookies from Chrome, generates SAPISIDHASH auth, and writes ytmusicapi headers. This runs automatically on connection checks.

### Deno for yt-dlp PO tokens
YouTube requires Proof of Origin tokens for some downloads. yt-dlp uses `remote_components: ["ejs:github"]` which requires deno to be installed. The `app.py` startup adds `~/.deno/bin` to PATH.

### MusicBrainz for genres instead of Spotify
Spotify's album genre endpoint (`/v1/albums/{id}`) almost always returns an empty array. The artist genre endpoint works but returns broad genres. MusicBrainz provides better genre data via community tags, so the tagger service falls back to MusicBrainz when Spotify genres are unavailable.

### No MUSIC/ prefix on SD card
The H2 scans the entire SD card for audio files and builds its Category browser from ID3 tags, not folder paths. Artist folders go directly at the SD card root. The `build_device_path()` function in `services/device.py` implements this.

### SQLite with async SQLAlchemy
Chose SQLite for simplicity (single-user app). Uses `aiosqlite` for async compatibility with FastAPI. The database lives at `data/leasure.db`.

### htmx for UI interactivity
The frontend uses htmx for partial page updates. Routers have both JSON API endpoints and `/html` endpoints that return Jinja2-rendered HTML partials. The SSE sync progress stream (`/api/device/sync/stream`) uses `StreamingResponse`.

### Sidecar files for H2
The H2 reads `.jpg` sidecar files (same name as audio) for album art and `.lrc` files for synced lyrics. These are created alongside the audio files in the library and copied during sync.

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
  downloader.py     -- Download dispatcher (spotdl_engine, ytdlp_engine, lossless fallbacks)
  spotdl_engine.py  -- Main download engine: ytmusicapi search + yt-dlp + Chrome cookies
  ytdlp_engine.py   -- Direct yt-dlp for YouTube-sourced tracks
  streamrip_engine.py -- Qobuz/Tidal/Deezer lossless (optional, requires credentials)
  bandcamp_engine.py  -- Bandcamp lossless fallback (optional)
  archive_engine.py   -- Internet Archive lossless fallback (optional)
  tagger.py         -- Full metadata pipeline: ID3/Vorbis tags, embedded art, sidecar jpg/lrc
  lyrics.py         -- lrclib.net synced lyrics fetcher
  artwork.py        -- Album art download + Pillow resize
  device.py         -- WSL2 device detection, FAT32 filename sanitization, device path builder
  playlist.py       -- M3U playlist generation (H2 format)

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

### Run the development server
```bash
source .venv/bin/activate
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

- Runs on WSL2 (Ubuntu) with Windows drives mounted at /mnt/
- Python 3.11+ with venv at `.venv/`
- ffmpeg must be installed (`sudo apt install ffmpeg`)
- deno must be installed for yt-dlp PO tokens
- Chrome must be installed and logged into YouTube Music for Premium quality
