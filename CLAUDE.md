# Leasure -- Claude Code Context

## Project Overview

Leasure is a local music downloader and library manager designed specifically for the HIFI WALKER H2 portable music player. It runs as a FastAPI web server on native Windows, plain Linux, or WSL2 (see the platform layer below), provides a browser-based UI for browsing Spotify and YouTube Music libraries, downloads audio via yt-dlp, applies H2-compatible metadata, and syncs to the H2's SD card.

The owner's H2 runs firmware V2.3 (hw4) on the HiBy platform.

The target user has Spotify Premium and YouTube Music Premium subscriptions. The app uses these accounts for library browsing and leverages YouTube Music Premium for higher quality audio via Chrome cookie extraction.

## Key Architecture Decisions

### Platform layer (Windows / Linux / WSL2)
`services/platform.py` detects the runtime platform (`get_platform()` returns `windows` | `wsl2` | `linux`). Device detection in `services/device.py` dispatches to one of three backends: Win32 drive enumeration via ctypes (Windows), `/media` + `/run/media` mount scanning (Linux), or `/mnt/<letter>` drvfs scanning (WSL2). Mounting only applies on WSL2 — Windows and desktop Linux auto-mount removable drives, and `POST /api/device/mount`, `/eject` and `/automount` are no-ops there. On WSL2 `services/wsl_mount.py` lists the Windows card-reader slots through PowerShell (`Win32_LogicalDisk`) and mounts/ejects `/mnt/<letter>` (drvfs, owned by the user) without a password: root comes from `wsl.exe -d <distro> -u root -e` through Windows interop, `sudo -n` as fallback, and only allow-listed absolute binaries (`mkdir`, `mount`, `umount`, `rmdir`) run as root. The Device page's Card Reader (`GET /api/device/removable/html`, `partials/removable_drives.html`, `partials/mount_result.html`) shows the slots; `POST /api/device/automount` (Device panel opened, Scan) mounts inserted cards and clears stale mounts of pulled cards. Mount and eject refuse the system drive, eject is refused while a sync writes to the card, and without a working root helper the result shows copy-paste commands. Templates receive `platform` and `device_path_placeholder` as Jinja globals. The cookie-extraction browser is configurable via `COOKIE_BROWSER` (default chrome) because Chrome 127+ app-bound encryption can block yt-dlp cookie extraction on native Windows (Firefox works there). Install scripts live in `scripts/` (install.sh/run.sh for Linux/WSL2, install.ps1/run.ps1 for Windows).

### yt-dlp instead of spotDL, in one engine
SpotDL was the original download engine but caused compatibility issues (dependency conflicts with the main app's Python environment, unreliable matching). The project now uses yt-dlp directly with ytmusicapi for search. `services/yt_engine.py` is the single pipeline for both Spotify-sourced and YouTube-sourced tracks (the old `spotdl_engine.py` / `ytdlp_engine.py` pair, which duplicated it, is gone). It resolves the source (an exact `youtube_id` wins over any search; otherwise scored ytmusicapi results, then scored `ytsearch5` results, with variant penalties and a minimum score -- no blind `ytsearch1`), downloads into `downloads/<track_id>/` via `extract_info(download=True)`, reads the provenance from `info["requested_downloads"][0]` (never by re-probing the output), verifies the duration within 10 s, then moves the file atomically into the library and removes the staging dir. There is no "most recent file in the folder" fallback. `Track.quality` stores the result tier (`aac_256`, `mp3_320`, ...) next to `source_format_id`, `source_codec`, `source_bitrate_kbps`, `source_sample_rate`, `premium_used`, `transcoded`, `is_lossless`, `verification`, `matched_title` and `match_score`. Lossy FLAC is no longer offered anywhere: `flac_lossless` is served by Qobuz only (see "Qobuz lossless engine") and errors or stays queued instead of falling back to a lossy source. Authenticated attempts are throttled and capped by `YT_MAX_DOWNLOADS_PER_DAY` (counter in `data/yt_daily.json`); a "Sign in to confirm" / 403 refusal stops the download and asks for fresh cookies instead of retrying cookie-less.

### Chrome cookie auto-refresh for YouTube Music
YouTube Music blocks unauthenticated and stale-cookie requests aggressively. The `youtube_client.py` has `_refresh_from_chrome()` which uses yt-dlp's cookie extraction to pull fresh cookies from Chrome, generates SAPISIDHASH auth, and writes ytmusicapi headers. This runs automatically on connection checks.

### Premium cookie gate
`data/cookies.txt` (Netscape format) is the credential yt-dlp authenticates with, not a browser profile: WSL2 has no readable profile and Chrome 127+ app-bound encryption blocks extraction on native Windows. `services/cookies.py` writes it from the `cookie` header of the manual header-paste flow (allow-listed Google/YouTube session cookies only, 0600, atomic), tracks its age (stale after 12 h) and source in `cookies.txt.meta.json`, and `premium_check()` proves the session really gets Premium audio by asking yt-dlp for the formats of `PREMIUM_CHECK_VIDEO_ID` on a `music.youtube.com` URL and looking for itag 141/774 or a "Premium" format note. The verdict is cached in `data/premium_check.json`, shown as PASS/FAIL on the YouTube Music card (`GET/POST /api/youtube/premium-check…`) and available as `python -m scripts.premium_check` (exit 1 on FAIL). Nothing downstream should claim Premium quality unless this passes.

The cookie file no longer has to be pasted. `scripts/launch.ps1` opens Leasure in a Chrome/Edge `--app` window with its own profile (`data/browser-profile/`) and a loopback `--remote-debugging-port`, and records it in `data/browser-cdp.json` (`{"port", "pid", "browser", "started_at"}`, removed when the window closes). `services/browser_session.py` consumes that file: `cdp_info()` only trusts a port in 1024-65535 that answers `GET http://127.0.0.1:<port>/json/version` with a loopback `webSocketDebuggerUrl`, `get_browser_cookies()` reads the window's cookies with one `Storage.getCookies` over that socket (blocking `websockets.sync`, so route handlers call it in `asyncio.to_thread`), and `import_session()` turns them into `cookies.write_cookie_file(..., source="app-window")` plus `youtube_client.write_headers_from_cookie_pairs()` (the shared SAPISIDHASH builder) and runs the Premium check. `ensure_fresh_cookies()` re-imports silently from inside `build_download_attempts` when the file is missing, older than 6 h or not from the window. Where the server cannot dial that port -- WSL2 with NAT networking -- `launch.ps1` reads the cookies itself (`Sync-BrowserCookies`, .NET `ClientWebSocket`) and relays them through `data/browser-cookies.json` (`{"fetched_at", "browser", "cookies"}`, rewritten every 5 minutes and whenever Python touches `data/browser-cookies.request`), which `relay_cookies()` accepts while it is under 10 minutes old, so `get_session_cookies()` / `session_status()` report `via: "cdp" | "relay" | null`. The card (`GET /api/youtube/session/status`, `POST /api/youtube/session/import`) leads with "Sign in inside Leasure" when a window is present and keeps the header paste as a collapsed fallback. Security: the debugging port is loopback-only, but any local process running as the owner can read that window's cookies through it -- the same trust level as `data/cookies.txt` on disk; the profile directory is gitignored with the rest of `data/`.

### What the H2 plays
Per its manual the H2 plays WAV, FLAC (16/24-bit up to 192 kHz), APE, DSD and MP3 -- not AAC/M4A (nor Opus/OGG). YouTube's best streams are AAC/Opus, so the only way to put better-than-MP3 audio on the card is a real lossless source: that is why "Best" asks Qobuz first and why FLAC is capped at 24/192.

### Qobuz lossless engine (opt-in)
Off until the owner signs in; the owner accepted the Qobuz ToS risk. `services/qobuz_client.py` is a direct, synchronous `httpx` client (callers use `asyncio.to_thread`) with three `httpx.Client`s -- API, web player, CDN -- and the token header only on the API host. Not streamrip: it cannot share the venv (Pillow<11 and pytest-asyncio<0.22 against Leasure's pins), its PyPI release predates the 2026 API fixes, its email/password login has been dead since about 2026-04 (Qobuz answers 401), and it swallows errors, discards the delivered depth/rate and forces its own tagging. Login is `user_id` + `user_auth_token` copied from play.qobuz.com DevTools → Local Storage → `localuser` (`id`, `userAuthToken`; Google sign-in leaves no password anyway), pasted as the two fields or the whole `localuser` JSON into the Qobuz card on Home (`POST /api/qobuz/session`; `DELETE` only forgets it locally). `user/login` is tried in three shapes (header POST, form POST, query GET), the working one cached as `login_shape`; a rotated token is stored; a 401 expires the session only when a confirming login also answers 401. `app_id` and the signing secrets are scraped from the web player (`play.qobuz.com/login` → `/resources/<version>/bundle.js`, appId regex plus `initialSeed`/timezone base64 secrets) and cached in `data/qobuz_app.json`; an `app_id` 400 or a rejected signature re-scrapes at most once per 30 min per process, secrets are tried in order and the working one is remembered. `track/getFileUrl` carries `request_sig = md5("trackgetFileUrlformat_id{fmt}intentstreamtrack_id{id}{ts}{secret}")`.

`services/qobuz_session.py` keeps the sign-in in `data/qobuz_session.json`, written by `services/secret_file.write_secret` (atomic; hardened while still empty: `chmod 600` on Linux, `icacls /inheritance:r` on native Windows and on drvfs via `icacls.exe`; `file_protected` records whether it worked). Every mutation holds one lock, re-reads the file and is a no-op if the token fingerprint changed; sign-out bumps a generation so running clients stop (`SessionChanged`); `status()` never returns the token. `qobuz_client.install_log_redaction()` (called from `app.py`) installs a log-record factory that runs every message, exception and stack text through `redact()`: registered secrets (token, app secrets), `request_sig=`, `user_auth_token=`, `X-User-Auth-Token` and the query of signed CDN URLs; `httpx`/`httpcore` log at WARNING, client exceptions carry fixed sentences raised `from None`, and the worker redacts every error it stores.

Routing (`services/downloader.py`): while a row is pending `track.quality` holds the request key. `native` ("Best") tries Qobuz, then the YouTube pipeline (a 429 cooldown defers it instead), recording why in `Track.qobuz_skip_reason` (`not_configured|signed_out|ineligible|cap|no_match|not_streamable|unavailable|cdn_unavailable`); `flac_lossless` is Qobuz only (a skip is an error, cap/breaker/cooldown keep it queued); `mp3_320` never touches Qobuz or its cap; an unknown key fails rather than becoming MP3. A network failure or damaged file is an error, not a fallback. Matching (`qobuz_engine.resolve_matches`): a stored `qobuz_id` matched by id/ISRC wins (`track/get`), else an exact-ISRC search, else one search `"<primary artist> <title>"`. Every candidate must be streamable, title ratio ≥ 0.9 (benign "remaster"/"album version" stripped), primary artist contained, duration within 10 s, same number tokens, no variant word (live, remix, acoustic, edit...) the request lacks; search hits score via `yt_engine.score_candidate` (≥ 4.0 downloads, ≥ 5.0 upgrades, which also need album ratio ≥ 0.6 and a length within 3 s of the current file), +0.5 × album ratio, hi-res breaks ties; up to 3 candidates are tried when one is not streamable. Quality: request `QOBUZ_MAX_FORMAT` (27 = ≤24/192, 7 = ≤24/96, 6 = 16/44.1); above 192 kHz retries at 7; a file too big for FAT32 steps down a format. A getFileUrl answer with `sample`, a ≤ 35 s duration for a > 60 s track, no URL, a non-FLAC mime or a format outside {6, 7, 27} is refused (a 30-second preview is never saved). The CDN must be https `*.akamaized.net`/`*.qobuz.com` (manual redirects, size ceiling, 20 min deadline). In staging (`downloads/<id>/source.flac`) Qobuz's tags/pictures are stripped, STREAMINFO checked (1-2 ch, 16/24-bit, ≤ 192 kHz), the whole file decoded by ffmpeg (`-err_detect crccheck+explode`, decoded length within 0.5 s), then tagged; audio and sidecars move into the library together. Result tier `flac_{bits}_{khz}`, `engine_used = "qobuz"`, plus `source_bit_depth`, `qobuz_id`, `qobuz_isrc`, `qobuz_match_method`.

Limits: one process-wide API throttle (`QOBUZ_API_CALLS_PER_MINUTE`, default 20, 1..40), a random 3-8 s gap between track fetches, and `QOBUZ_MAX_DOWNLOADS_PER_DAY` (default 100) streams, a slot reserved right before each CDN body; the counter and the 429 cooldown (`Retry-After` or 300 s) live in `data/qobuz_daily.json`. An in-memory breaker pauses Qobuz for 1 h on app/secret breakage or a 403 outside getFileUrl (`unavailable`) or 3 CDN/decode failures in a row (`cdn_unavailable`). Less than 2 × size + 1 GiB free in the download or library dir defers the track 15 min. Deferrals raise `CapDeferred`: the row stays pending, the worker's scheduler re-enqueues it when due (30 s tick), and its priority queue puts downloads ahead of upgrades.

Upgrades ("Upgrade to FLAC", `POST /api/qobuz/upgrade/tracks|all|track/{id}`, `/upgrade/revert/{id}`, `/upgrade/dismiss/{id}`) set `upgrade_state = "queued"` on eligible finished tracks (`Track.upgrade_eligibility`); the worker feeds them only while no download waits and only up to today's remaining cap. Under `services/track_locks.hold(track_id)` the swap moves the old audio and sidecars into `data/upgrades/<id>/old/` (copied when another row shares the file), journaling each step in `data/upgrades/<id>/journal.json`, then commits with a compare-and-set on the row or rolls back. The old files stay `QOBUZ_UPGRADE_KEEP_DAYS` (default 30, `upgrade_backup_until`) for Revert. `download_worker.start()` runs `recover_upgrade_journals()` (finish or undo an interrupted swap/revert) and `purge_old_backups()` even when the worker is disabled. A failed upgrade sets `upgrade_state = "error"` and leaves the track done.

Card side: each card gets `.leasure-card.json` (`card_id`) on its first sync, and every sync upserts a `CardCopy` (rel path, size, mtime, SHA-256 of the first 1 MiB) for each file it wrote or found current; card writes go to `<name>.leasure-part` and are renamed into place. An upgrade or revert records `SupersededFile` rows (that track's recorded card copies, plus a byte identity of the replaced library file for any card). A final pass after the playlists, whatever the scope, deletes only a superseded file at a safe `Artist/Album/File` path that no library row maps to, whose size, head hash and mtime (±2 s, ±1 h for DST) are what Leasure wrote, and only once its replacement is current on that card; the rest is kept and listed (`waiting`, `path_in_use`, `changed_on_card`) in the `done` event (`removed`, `removed_paths`, `kept`, `kept_paths`) and the sync diff; playlists point at the old copy until the new one is there. Before copying, `_plan_space` walks the files in sync order rounding each to the card's cluster size (`statvfs` / `GetDiskFreeSpaceW`, 32 KiB default), counts new folders, keeps the replaced file during its `.leasure-part` copy, credits nothing for the cleanup and adds 64 MiB; if it does not fit the sync errors before writing anything.

`TrustedHostMiddleware` (`ALLOWED_HOSTS`, default `127.0.0.1`, `localhost`) is added last so it runs first: a DNS-rebinding page reaches 127.0.0.1 under its own host name and is refused. Qobuz routes answer with `HX-Trigger: qobuz:changed` on sign-in/out and `qobuz:queued` (`{"track_ids": [...]}`) on queue/revert; the library and album views listen for them. The card's check (`GET/POST /api/qobuz/check`, cached in `data/qobuz_check.json`) logs in and resolves -- never downloads -- the `QOBUZ_CHECK_QUERY` track. `python -m scripts.qobuz_check [--isrc ISRC | --query Q] [--format 27] [--probe-cdn] [--save-fixtures [DIR]]` prints a redacted JSON report (app id, bundle version, secret index, login shape, plan, stream); `--probe-cdn` reads the first 64 KiB of the stream and saves nothing; exit 0 when login works, the stream is full lossless FLAC and a secret signs, 1 otherwise, 2 without a sign-in. Known risk: the client depends on the web player's bundle layout and login, so a Qobuz web-player change can break sign-in or signing until the regexes/shapes are updated; Best then falls back to YouTube and FLAC-only waits.

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
`POST /api/device/sync/start` validates the target with `resolve_sync_target()` (must be a volume `detect_devices()` found, never `device_type == "system"`) and returns a single-use job id; `GET /api/device/sync/stream/{job_id}` streams SSE progress. Tracks copy album by album in disc/track order; a file whose card copy has the same size and mtime (within 2 s, FAT rounding) is skipped, and the copy gets the source mtime so the next sync recognises it; the `done` event carries `copied` and `skipped`. On a multi-disc album (`total_discs > 1` or disc > 1) names get a `D-NN - ` prefix instead of `NN - `; dotted titles ("Mr. Brightside") keep their full name (`with_audio_suffix`, not `with_suffix`). The old `GET /sync/stream?device_path=` was CSRF-able (a drive-by page could copy the library anywhere and delete playlists) and is gone. `SameOriginMiddleware` in `app.py` additionally rejects mutating requests the browser marks cross-site.

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
models.py           -- ORM models: Track, Playlist, PlaylistTrack, SyncHistory, CardCopy, SupersededFile
worker.py           -- Async download queue (priority queue: downloads before FLAC upgrades; deferral scheduler)

routers/
  spotify.py        -- Spotify browse + download (htmx + JSON endpoints)
  youtube.py        -- YouTube Music browse + download
  device.py         -- Device detection, sync (SSE stream), diff, file browser
  downloads.py      -- Download queue status
  library.py        -- Library browsing
  qobuz.py          -- Qobuz card (sign-in, check) and FLAC upgrade / revert endpoints
  art.py            -- Same-origin cover-art proxy (allow-listed Spotify/YouTube/Qobuz image hosts)

services/
  browser_session.py -- Reads the YouTube login from Leasure's own browser window over CDP
  spotify_client.py -- Spotipy OAuth wrapper
  youtube_client.py -- ytmusicapi wrapper with Chrome cookie auto-refresh
  downloader.py     -- Routes by requested format: Qobuz first for Best/FLAC-only, yt_engine otherwise
  yt_engine.py      -- The YouTube pipeline: scored match, staging dir, provenance, atomic move
  ytdlp_opts.py     -- Shared yt-dlp option sets (cookie file, throttle, PO-token solver)
  qobuz_client.py   -- Direct Qobuz API client (sync httpx): app id/secret scrape, signing, CDN download, log redaction
  qobuz_session.py  -- Stored Qobuz sign-in (data/qobuz_session.json), check, status
  qobuz_engine.py   -- Qobuz FLAC downloads (gate, matching, verification) and journaled, revertible FLAC upgrades
  secret_file.py    -- Atomic owner-only writes for credential files (chmod 600 / icacls)
  track_locks.py    -- Per-track asyncio lock for anything that moves a track's library file
  bandcamp_engine.py  -- Bandcamp lossless stub (not called)
  archive_engine.py   -- Internet Archive lossless stub (not called)
  tagger.py         -- Full metadata pipeline: ID3/Vorbis tags, embedded art, sidecar jpg/lrc
  lyrics.py         -- lrclib.net synced lyrics fetcher
  artwork.py        -- Album art download + Pillow resize
  platform.py       -- Runtime platform detection (windows | wsl2 | linux)
  formats.py        -- Shared download format/quality mapping
  device.py         -- Device detection (per-platform backends), FAT32 filename sanitization, device path builder
  wsl_mount.py      -- WSL2 passwordless card mount/eject via `wsl.exe -u root` (sudo -n fallback)
  card_import.py    -- Rebuilds the library from the songs on an H2 card (card is only read)
  playlist.py       -- M3U playlist generation (H2 format) + on-card manifest of generated files
  oauth_state.py    -- one-shot OAuth state tokens shared by Spotify and Google flows

scripts/
  import_card.py    -- Import the songs on an H2 card into the library (`--source`, `--dry-run`; report in data/card-import/)
  move_library.py   -- Move the library to DEST with size+SHA-256 checks, rewrite Track.file_path (`--dry-run`, `--force`)
  clean_card.py     -- Delete the card copies an import made stale, after a sync; dry run unless `--apply`
  qobuz_check.py    -- Redacted report of what Qobuz answers for the stored sign-in (`--probe-cdn`; exit 0/1/2)

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

### Qobuz sign-in expired / app secret rotation
- "Your Qobuz sign-in expired": a confirmed 401 marked `data/qobuz_session.json` expired; Best falls back to YouTube (`qobuz_skip_reason = signed_out`) and FLAC-only tracks fail. Sign in again on play.qobuz.com, copy `localuser` again and paste it into the Qobuz card. Signing out in Leasure only forgets the token; sign out on play.qobuz.com to end the session.
- "Qobuz changed its web player and Leasure cannot sign requests right now": the bundle's `app_id`/secrets rotated or the scrape regexes stopped matching. The client re-scrapes on its own (at most once per 30 min) and the breaker pauses Qobuz for an hour (`unavailable`; a server restart clears it). `python -m scripts.qobuz_check` shows `bundle_version`, `secrets_found`, `secret_index` and `login_shape`; deleting `data/qobuz_app.json` forces a fresh scrape on the next client. If the scrape finds nothing, `parse_bundle_url` / `parse_app_credentials` in `services/qobuz_client.py` need updating.

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
`tests/conftest.py` sets `DATA_DIR`/`LIBRARY_DIR`/`DOWNLOAD_DIR` env vars before importing `config`, and the `fake_device` fixture monkeypatches `detect_devices()` so sync tests get a temp "H2". An autouse guard in `conftest.py` (`assert_isolated_paths`, covered by `tests/test_isolation.py`) stops the run with exit 2 when `data_dir`/`library_dir`/`download_dir` or the database file are not temp paths outside the repo. Never run pytest or app scripts against the real `data/` or library: a stray run once emptied the real DB.

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
- Python 3.11+ with venv at `.venv/` (created by `scripts/install.sh` / `scripts/install.ps1`); on this WSL2 checkout the venv lives at `~/.venvs/leasure` (kept on the Linux filesystem) — start with `LEASURE_VENV=~/.venvs/leasure scripts/run.sh`
- ffmpeg and deno are installed user-side here (`~/.local/bin/ffmpeg`, `~/.deno/bin/deno`) because sudo needs a password
- ffmpeg must be installed (`sudo apt install ffmpeg` / `winget install Gyan.FFmpeg`)
- deno must be installed for yt-dlp PO tokens
- A browser logged into YouTube Music for Premium quality (Chrome by default; set `COOKIE_BROWSER=firefox` on native Windows if Chrome cookie extraction is blocked)
- The checkout lives at `C:\dev\leasure` (`/mnt/c/dev/leasure`), out of OneDrive since 2026-10-17; `data/` and `downloads/` stay in the repo
- The song library stays in OneDrive on purpose so other Windows PCs can reach it: `LIBRARY_DIR=/mnt/c/Users/pepe_/OneDrive/Música/Leasure` in `.env`, pinned "Always keep on this device" (dehydrated OneDrive files cause intermittent EIO through the WSL drvfs bridge)
