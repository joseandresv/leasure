# Leasure

A local music downloader and library manager built for the **HIFI WALKER H2** portable music player. Browse your Spotify and YouTube Music libraries through a web UI, download tracks via YouTube Music (with optional Premium quality), and sync everything to your H2's SD card with proper metadata, album art, synced lyrics, and playlists. [Music Menu from DanceDanceRevolution inspired UI]

## Features

- **Spotify library browsing** -- saved albums, playlists, liked songs via Spotify Web API (spotipy)
- **YouTube Music library browsing** -- playlists, albums, liked songs via ytmusicapi
- **YouTube Music Premium quality** -- automatic Chrome cookie extraction for higher bitrate downloads via yt-dlp
- **Unified "Recently Listened"** -- merges true listening history from three sources, sorted by real recency:
  - Spotify `current_user_recently_played` (precise ISO timestamps) plus the currently-playing track pinned at the top
  - YouTube Music history via ytmusicapi (`Today`/`Yesterday`/date bucket timestamps)
  - Plain YouTube watch history via Google OAuth2 + InnerTube `FEhistory` browse on the `TVHTML5` client (the only combo that accepts OAuth Bearer tokens)
  - Cross-source dedup with max-timestamp merging, per-track `via {provider}` label, and a refresh button
- **MusicBrainz genre lookup** -- fetches artist genres from MusicBrainz when Spotify's album genre endpoint returns empty (which it usually does)
- **Synced lyrics** -- fetches time-synced `.lrc` lyrics from lrclib.net and saves them as sidecar files
- **H2-compatible metadata** -- writes proper ID3v2.4 tags (MP3) and Vorbis comments (FLAC), embeds album art, and creates sidecar `.jpg` and `.lrc` files that the H2 reads natively
- **Device sync with progress** -- detects mounted drives on Windows (drive letters), Linux (`/media`, `/run/media`), and WSL2 (`/mnt/`), copies music with SSE progress streaming, generates `.m3u` playlists at the SD card root
- **Playlist generation** -- `.m3u` files placed at the SD card root for H2 compatibility
- **Background download queue** -- async worker processes downloads without blocking the UI
- **Album art carousel** -- homepage shows spinning LP artwork from your downloaded collection

## Architecture

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Jinja2 templates + htmx |
| Database | SQLite via SQLAlchemy async + aiosqlite |
| Spotify API | spotipy (OAuth2 PKCE) |
| YouTube Music API | ytmusicapi (browser cookie auth) |
| YouTube history API | Google OAuth2 + InnerTube `TVHTML5` client |
| Audio download | yt-dlp (with optional deno for Premium PO token) |
| Audio tagging | mutagen (ID3v2.4 for MP3, Vorbis for FLAC) |
| Artwork processing | Pillow (resize to 500x500 JPEG) |
| Lyrics | lrclib.net REST API |
| Genre lookup | MusicBrainz REST API |
| HTTP client | httpx (async) |

### How downloads work

One pipeline handles every YouTube download (`services/yt_engine.py`):

1. User selects a track/album/playlist from the Spotify or YouTube Music browser
2. Track metadata is saved to SQLite and enqueued in the async download worker
3. Source resolution: a known `youtube_id` is used as-is; otherwise YouTube Music (ytmusicapi) is searched and the results are scored on title, artist and duration. Live/remix/cover/karaoke/instrumental variants the request did not ask for are penalised, a duration more than 30 s off is disqualifying, and nothing below the minimum score is accepted. If YouTube Music has no confident match, five plain-YouTube results are scored the same way -- a track fails with "No confident match" rather than downloading the wrong recording
4. yt-dlp downloads into a staging directory (`downloads/<track_id>/`) with the cookie file from `data/` (or the configured `COOKIE_FILE`). The stream preference is `141` (AAC 256, Premium) then `774` (Opus, Premium) then any M4A. When YouTube refuses the logged-in session the download stops and asks for fresh cookies; it never retries anonymously to fetch a quieter 128 kbps copy
5. Provenance is read from yt-dlp's info dict (`format_id`, `acodec`, `abr`, `asr`, `format_note`) -- the file is never re-probed to guess what it was. The reported duration is checked against the requested track (within 10 s) and recorded as `verification`
6. The finished file is moved atomically out of staging into the library and the staging directory is removed; there is no "most recent file in the folder" guesswork
7. The tagger service applies full tags, embeds album art, creates sidecar `.jpg`, and fetches `.lrc` lyrics
8. Genre is looked up from MusicBrainz if not already known

`Track.quality` records the **result** (`aac_256`, `aac_128`, `opus_160`, `mp3_320`), alongside
`source_format_id`, `source_codec`, `source_bitrate_kbps`, `source_sample_rate`, `premium_used`,
`transcoded`, `is_lossless`, `verification`, `matched_title` and `match_score`.

Formats offered are "Best available" (the source stream, remuxed, no re-encode), MP3 320, and
FLAC lossless from a real lossless service. The old "FLAC (transcoded)" option is gone: a FLAC
container filled from a YouTube stream claims a fidelity the source never had, and `flac_lossless`
now fails with a clear message instead of silently transcoding a lossy source.

The cookie file is the owner's Google session, so authenticated downloads are throttled
(10-30 s between tracks, 5 MB/s ceiling, 3 retries) and capped at `YT_MAX_DOWNLOADS_PER_DAY`
(default 50, counted in `data/yt_daily.json`). Over the cap tracks stay queued and resume the
next day instead of failing.

### File organization

Library files are stored as:
```
library/{Artist}/{Album}/{NN} - {Title}.mp3
library/{Artist}/{Album}/{NN} - {Title}.jpg   (sidecar album art)
library/{Artist}/{Album}/{NN} - {Title}.lrc   (synced lyrics)
```

On the H2 SD card, the same structure is used at the root (no `MUSIC/` prefix needed -- the H2 scans the entire card and uses ID3 tags for its Category browser).

## Installation

Leasure runs on three platforms via a runtime platform layer (`services/platform.py`): **native Windows**, **plain Linux**, and **WSL2**. Device detection adapts automatically to each; the manual drive-mount UI only appears on WSL2.

### Prerequisites (all platforms)

- **Python 3.11+**
- **ffmpeg** -- required by yt-dlp for audio conversion
- **deno** -- required by yt-dlp for YouTube Premium PO token generation
- **A browser logged into <https://music.youtube.com>** (Chrome by default) -- used for Premium quality downloads and automatic cookie refresh

### Linux / WSL2 quick start

Install ffmpeg and deno first:

```bash
sudo apt install ffmpeg        # Debian/Ubuntu
sudo dnf install ffmpeg        # Fedora
curl -fsSL https://deno.land/install.sh | sh
```

No sudo? Both tools also install fine into your home directory: a static ffmpeg build
(<https://johnvansickle.com/ffmpeg/>) copied to `~/.local/bin`, and the deno release zip
(<https://github.com/denoland/deno/releases>) extracted to `~/.deno/bin` (the app adds
`~/.deno/bin` to `PATH` at startup).

Then clone and install:

```bash
git clone <repo-url> leasure
cd leasure
scripts/install.sh     # creates .venv, installs requirements, checks ffmpeg/deno, copies .env.example to .env
# edit .env (see Configuration below)
scripts/run.sh
```

If the checkout lives in a OneDrive folder or on a Windows drive under WSL2, put the
virtual environment somewhere else -- both scripts honour `LEASURE_VENV`:

```bash
LEASURE_VENV=~/.venvs/leasure scripts/install.sh
LEASURE_VENV=~/.venvs/leasure scripts/run.sh
```

### Windows (native) quick start

Install Python 3.11+ from <https://python.org> (or `winget install Python.Python.3.12`), then:

```powershell
winget install Gyan.FFmpeg DenoLand.Deno
git clone <repo-url> leasure
cd leasure
scripts\install.ps1    # creates .venv, installs requirements, checks ffmpeg/deno, copies .env.example to .env
# edit .env (see Configuration below)
scripts\run.ps1
```

If PowerShell refuses to run the scripts, allow them for the current session first:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### Desktop shortcut (Windows)

Put a Leasure icon on the Windows desktop:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

Then double-click **Leasure**. The shortcut runs `scripts\launch.ps1`, which reuses the
server if one already answers on the port, otherwise starts it -- from `.venv` on a native
Windows install, or inside WSL2 when the checkout only has a Linux venv (WSL forwards the
port, so <http://localhost:8642> works either way) -- and opens Leasure in a Chrome or Edge
app window once it responds.

**Closing the app window stops the server**, but never mid-job: the launcher waits until
the download queue and any device sync have finished (re-checking every 10 s) and then
shuts the server down. If the server was already running before you double-clicked, the
window closing leaves it alone.

The app window uses its own browser profile in `data/browser-profile/`, so it has none of
your bookmarks, extensions or logins. It also exposes a loopback-only DevTools port (written
to `data/browser-cdp.json` while the window lives) that Leasure uses to read your YouTube
Music login from that window. When the server runs inside WSL2 with NAT networking it cannot
reach that port, so the launcher reads the cookies itself and relays them through
`data/browser-cookies.json` (refreshed on request and every 5 minutes; both files are deleted
when the window closes).

To keep the server running instead, start it with `-NoAppWindow`: it opens your default
browser in a normal tab and leaves the server up (the same happens if no Chrome or Edge is
found). Stop it again with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\launch.ps1 -Stop
```

Options: `-Port 8645` for another port, `-NoAppWindow` to use the default browser,
`-NoBrowser` to start the server only, `-StartMenu` (on `create_shortcut.ps1`) to add a
Start menu entry. Set `LEASURE_BROWSER` to a Chromium-based exe to host the app window with
something other than Chrome or Edge, and `LEASURE_WSL_DISTRO` if the Leasure checkout lives
in a distro other than the default one.

### Manual setup (any platform)

If you'd rather not use the install scripts:

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/WSL2
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
cp .env.example .env           # copy .env.example .env on Windows
```

## Platform notes

### Windows

- Chrome 127+ uses app-bound cookie encryption, which can block yt-dlp's cookie extraction. If YouTube Music Premium quality fails, set `COOKIE_BROWSER=firefox` in `.env` (and log Firefox into music.youtube.com), or use the manual header-paste flow on the app's YouTube Music page.
- Removable devices show up automatically as drive letters -- no mounting needed.

### Plain Linux

- Removable drives auto-mount under `/media/<user>/` or `/run/media/<user>/` on desktop distros; the app picks them up automatically.
- On headless boxes with no browser, use the manual header-paste flow for YouTube Music authentication.

### WSL2

- Removable drives must be mounted manually (`sudo mount -t drvfs E: /mnt/e`). The app's Device page has a **Mount** button that does this for you.
- Do **not** keep the repo (or its `.venv`/`library`) inside a OneDrive-synced folder -- OneDrive Files-On-Demand causes intermittent I/O errors through the WSL drvfs bridge. If you must, right-click the folder in Explorer and choose **"Always keep on this device"**.

## Configuration

### Configure Spotify

1. Go to <https://developer.spotify.com/dashboard>
2. Create a new application
3. Set the redirect URI to `http://127.0.0.1:8642/api/spotify/callback`
4. Copy the Client ID and Client Secret into your `.env` file:

   ```env
   SPOTIFY_CLIENT_ID=your_client_id_here
   SPOTIFY_CLIENT_SECRET=your_client_secret_here
   ```

The Spotify OAuth flow requests the following scopes: `user-library-read`, `playlist-read-private`, `playlist-read-collaborative`, `user-read-private`, `user-read-recently-played`, `user-read-currently-playing`, and `user-read-playback-state`. The last three are required for the Recently Listened feed and the currently-playing pinned track.

### Configure YouTube Music

#### Sign in inside Leasure (recommended)

Start Leasure from the desktop shortcut (`scripts/launch.ps1`). It opens the app in its own
Chrome/Edge window with a profile of its own (`data/browser-profile/`) and a DevTools port on
`127.0.0.1`, recorded in `data/browser-cdp.json`. Because that window belongs to Leasure, the app
can read the login out of it -- no header pasting, no cookie export:

1. On the home page, the YouTube Music card offers **Open YouTube Music and sign in** -- it opens
   `music.youtube.com` in a second window of the same profile
2. Sign in there (this profile is separate from your everyday browser, so it asks once)
3. Back on the card, press **I'm signed in -- use this login**

The cookies are written to `data/cookies.txt`, the ytmusicapi headers to
`data/youtube_headers.json`, and the Premium check runs immediately so the card shows the real
verdict. Downloads re-read the login from the window whenever the cookie file is older than
6 hours, so a window that stays signed in keeps working without any further clicks. On a WSL2
install the launcher hands the cookies over through `data/browser-cookies.json` instead, which
works the same way but needs the app window (and so the launcher) to still be open.

#### Fallbacks

- **Header paste** -- the **How to set up** panel on the same card takes browser request headers
  (or a "Copy as cURL" command) and derives the same cookie file. Use it on headless hosts or when
  you did not start Leasure from the shortcut
- **A browser profile on disk** -- when no cookie file exists, yt-dlp extracts cookies from the
  browser set by `COOKIE_BROWSER` in `.env` (default `chrome`; `firefox`, `edge`, `brave`, ... also
  work). Chrome 127+ app-bound encryption often blocks this on native Windows:

  ```env
  COOKIE_BROWSER=firefox
  ```

#### Premium check

Premium audio (AAC 256, itag 141) is only served to a session YouTube recognises as a Premium subscriber, so "Premium quality" is worth verifying rather than assuming. The YouTube Music card on the home page shows **Premium: PASS / FAIL / not checked** with a **Check now** button, and the same verdict is available from the command line:

```bash
python -m scripts.premium_check          # exits 0 on PASS, 1 on FAIL
python -m scripts.premium_check --video <youtube-id>
```

The check downloads nothing: it asks yt-dlp for the formats of one known track on `music.youtube.com` using `data/cookies.txt`.

Authenticated downloads are capped per day by `YT_MAX_DOWNLOADS_PER_DAY` (default 50, counter in `data/yt_daily.json`); the worker leaves the rest queued once the cap is hit.

That cookie file is what yt-dlp authenticates with. It is written automatically whenever you connect through the header-paste flow, and you can also drop in an export from a browser extension such as "Get cookies.txt LOCALLY" (save it as `data/cookies.txt`, or set `COOKIE_FILE` to another path). Cookies older than 12 hours are flagged as stale in the card. **Treat the file as your Google session**: keep it private and out of any synced folder.

### Configure YouTube (plain) — optional, for watch-history feed

Plain YouTube watch history requires a Google OAuth2 flow (separate from YouTube Music). This is **optional** — skip this section if you only care about Spotify and YouTube Music history.

1. Go to <https://console.cloud.google.com/apis/credentials>
2. Create a project (or select an existing one)
3. Enable the **YouTube Data API v3** under APIs & Services → Library
4. Create an **OAuth 2.0 Client ID** of type **Web application**
5. Add `http://127.0.0.1:8642/api/youtube/oauth/callback` as an authorized redirect URI
6. On the OAuth consent screen, add the `https://www.googleapis.com/auth/youtube` scope (full `youtube`, not just `youtube.readonly` — the broader scope is required for the `FEhistory` InnerTube endpoint)
7. Copy the credentials into your `.env` file:

   ```env
   GOOGLE_CLIENT_ID=your_client_id_here.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your_client_secret_here
   ```

8. Restart the server and click **Connect YouTube** on the home page to authorize

Tokens are stored in `data/youtube_oauth.json` and auto-refresh using the refresh token on each call.

## Usage

Use the run script for your platform:

```bash
scripts/run.sh     # Linux / WSL2
```

```powershell
scripts\run.ps1    # Windows
```

Or start it manually:

```bash
# Activate the virtual environment
source .venv/bin/activate      # Linux/WSL2
# .venv\Scripts\activate       # Windows

# Start the server
python app.py
# or: uvicorn app:app --host 127.0.0.1 --port 8642 --reload
```

Open <http://127.0.0.1:8642> in your browser.

### Pages

- **Home** (`/`) -- library stats, album art carousel, and connection cards for Spotify / YouTube Music / YouTube
- **Music** (`/music`) -- unified browser across all sources with tabs:
  - **Recently Listened** -- true listening history merged from Spotify, YouTube Music, and plain YouTube, sorted by recency with a refresh button and per-track `via {provider}` label
  - **Albums**, **Playlists**, **Artists** -- unified views with cross-source dedup
- **Spotify** (`/spotify`) -- browse and download from your Spotify library
- **YouTube Music** (`/youtube`) -- browse and download from your YouTube Music library
- **Downloads** (`/downloads`) -- monitor download queue and status
- **Library** (`/library`) -- browse your downloaded music collection
- **Device** (`/device`) -- detect your H2, preview sync diff, sync with progress

### Downloading music

1. Navigate to the Music, Spotify, or YouTube Music page
2. Connect your account (Spotify via OAuth, YouTube Music via Chrome cookies, plain YouTube via Google OAuth2 if you want watch-history)
3. Browse your albums, playlists, or liked songs (or use the Recently Listened tab to grab what you've actually been playing)
4. Click download on individual tracks, full albums, or entire playlists — both the provider-specific endpoints and the unified `/api/music/download/{album,playlist}/{provider}/{id}` endpoints are wired up
5. Monitor progress on the Downloads page

### Syncing to H2

1. Connect your H2 via USB. On Windows it appears as a drive letter and on desktop Linux it auto-mounts under `/media`/`/run/media`; on WSL2 it must be mounted under `/mnt/` (use the Device page's Mount button or `sudo mount -t drvfs E: /mnt/e`)
2. Go to the Device page
3. Select your H2 from the detected devices list
4. Preview the sync diff to see what will be added
5. Click sync -- the app validates that the target is one of the detected removable volumes (never the OS drive), creates a sync job with a `POST`, and streams progress in real time via SSE
6. Playlists are generated as `.m3u8` files at the SD card root. Leasure records the files it wrote in a small `.leasure-playlists.json` manifest on the card and only ever removes those; playlists you put on the card yourself are left alone

## HIFI WALKER H2 Compatibility

The H2 is a portable HiFi music player that reads music from a micro SD card. Leasure is specifically built around its requirements:

### Metadata

- **ID3v2.4 tags** for MP3: TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TCON, TDRC, APIC
- **Vorbis comments** for FLAC and Opus: title, artist, album, albumartist, tracknumber, discnumber, genre, date
- **iTunes atoms** for native M4A/AAC downloads (`\xa9nam`, `\xa9ART`, `\xa9alb`, `aART`, `trkn`, `disk`, `\xa9gen`, `\xa9day`, `covr`)
- **Album artist** (TPE2) is always set -- the H2 uses this for its Category browser; defaults to the primary artist if not explicitly provided
- **One genre per file** -- the H2 groups its Genre category by the exact tag text, so only the top-ranked genre is written to the file; the full comma-separated list stays in the database for the genre map

### Sidecar files

- `.jpg` -- album art resized to 500x500, same filename as audio file. The H2 reads these for album art display.
- `.lrc` -- time-synced lyrics in LRC format. The H2 displays these during playback.

### Playlists

- `.m3u8` files (UTF-8 with BOM, CRLF) must be at the **root** of the SD card
- Paths inside are relative to the SD root with forward slashes (e.g., `Artist/Album/01 - Track.mp3`)
- The H2 shows these under its Explorer, not the Playlists category

### Folder structure

Artist folders go directly at the SD card root. No `MUSIC/` prefix is needed. The H2 scans the entire card and builds its Category view from ID3 tags, not folder structure.

### Filename sanitization

All filenames are sanitized for FAT32 compatibility (no `\/:*?"<>|` or control characters, Windows reserved names such as `CON`/`NUL` are prefixed, max 200 chars).

## Development

```bash
LEASURE_DEV=1 scripts/install.sh   # also installs pytest + ruff (requirements-dev.txt)
ruff check .
pytest -q
```

Tests run against a temporary data directory and an in-process ASGI client -- no server, browser or network is needed. The M4A tagging test synthesizes a file with ffmpeg and is skipped when ffmpeg is absent. CI (`.github/workflows/ci.yml`) runs the same lint + tests on Python 3.11 and 3.13.

## Security

- Leasure is a single-user local app: it binds to `127.0.0.1` and has no login. Do not expose the port to a network you do not control.
- State-changing requests are rejected when the browser marks them cross-site (`Sec-Fetch-Site` / `Origin` check), and the device sync is created with a `POST` whose target must be a detected removable volume, so a web page in another tab cannot start a sync or point it at your system drive.
- OAuth callbacks (Spotify, Google) require a one-shot `state` token issued by this process.
- Provider metadata (titles, artists, artwork URLs) is HTML-escaped wherever it is rendered.
- All Python packages in `requirements.txt` are pinned to minimum versions audited for known CVEs
- No credentials are stored in source code; all secrets go in `.env` (excluded from git)
- Spotify OAuth tokens are cached locally in `data/.spotify_cache`
- YouTube Music headers are stored locally in `data/youtube_headers.json`
- The app window's DevTools port (`data/browser-cdp.json`) listens on `127.0.0.1` only, and Leasure reads cookies from it rather than from your everyday browser profile. Any other process running as you could read that window's cookies through the same port -- the same trust level as `data/cookies.txt` sitting on disk. The window's profile lives in `data/browser-profile/` (gitignored); delete it to sign out
- YouTube (plain) OAuth tokens are stored locally in `data/youtube_oauth.json` and auto-refresh
- The server binds to `127.0.0.1` by default (localhost only)

## Project Structure

```text
leasure/
  app.py                  # FastAPI app, lifespan, page routes
  config.py               # Pydantic settings from .env (Spotify + Google OAuth creds)
  db.py                   # SQLAlchemy async engine + session
  models.py               # Track, Playlist, PlaylistTrack, SyncHistory
  worker.py               # Async download queue worker
  requirements.txt        # Pinned dependencies
  .env.example            # Environment template (no secrets)
  routers/
    spotify.py            # Spotify browsing + download endpoints
    youtube.py            # YouTube Music browsing + download + Google OAuth2 endpoints
    music.py              # Unified cross-source browser + Recently Listened feed
    device.py             # Device detection, sync, diff
    downloads.py          # Download queue monitoring
    library.py            # Library browsing
  services/
    spotify_client.py     # Spotipy wrapper (OAuth, library, recently_played, currently_playing)
    browser_session.py    # Reads the YouTube login from Leasure's own browser window over CDP
    youtube_client.py     # ytmusicapi + Google OAuth2 + InnerTube TVHTML5 history fetcher
    music_aggregator.py   # Unified Recently Listened: merges + dedupes + sorts by recency
    downloader.py         # Download dispatcher (routes to engines)
    yt_engine.py          # The YouTube pipeline: scored match, staging dir, provenance, atomic move
    ytdlp_opts.py         # Shared yt-dlp option sets (cookies, throttle, PO-token solver)
    streamrip_engine.py   # Lossless download via Qobuz/Tidal/Deezer (optional)
    bandcamp_engine.py    # Bandcamp lossless fallback (optional)
    archive_engine.py     # Internet Archive lossless fallback (optional)
    tagger.py             # Full H2 metadata: ID3 tags, embedded art, sidecar files, lyrics
    lyrics.py             # lrclib.net synced lyrics fetcher
    artwork.py            # Album art download + resize
    device.py             # Device detection, FAT32 sanitization, path building
    playlist.py           # M3U playlist generation for H2
  templates/              # Jinja2 HTML templates (htmx partials)
  static/                 # CSS, JS
  data/                   # SQLite DB, auth caches (gitignored)
  library/                # Downloaded music files (gitignored)
  downloads/              # Temp download staging (gitignored)
```

## License

Personal project. Not for redistribution.
