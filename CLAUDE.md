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
Per its manual the H2 plays WAV, FLAC (16/24-bit up to 192 kHz), APE, DSD and MP3 -- not AAC/M4A (nor Opus/OGG). YouTube's best streams are AAC/Opus, so the only way to put better-than-MP3 audio on the card is a real lossless source: that is why "Best" asks Qobuz first and why FLAC is capped at 24/192. Owner test 2026-10-18: the stock firmware's Music menu does not index `.m4a` files, but they play from its file browser; the owner chose to keep the existing `.m4a` files rather than convert them.

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

### Local albums and album info (a read-only overlay)
`services/local_albums.py` groups the library's `done` rows into albums by a frozen key: `a-` + 20 hex of sha1 over `KEY_VERSION`, `key_artist()` (album artist, else the track artist, first comma part -- the card-folder rule) and `norm_v2(album)` (NFKC, casefold, benign "2011 Remaster" / "Explicit" / "Album Version" words dropped). Rows with no album or "Unknown Album" form the one `loose` group ("Loose songs"). The normaliser deliberately shares nothing with the Qobuz, card-import or sync normalisers; changing its output needs a `KEY_VERSION` bump, and `album_info.rekey_stale_rows()` (run in the lifespan) re-keys stored rows from their raw names. `scan()` is cached on a fingerprint of the scanned columns; `find_key(name, artist)` maps a provider album to a local key (exact, or a name differing only by edition words when exactly one album of that artist fits).

`services/album_info.py` looks each album up once in a background job (one worker task, queue capped at 50, never awaited by a request, no DB session open during a network call): MusicBrainz first (search plus ISRC lookups, search score ≥ 80), then `services/deezer.py` (public API, no account, about 1 req/s; error codes 4/700 retried once after 5 s, 800 = no data) only when MusicBrainz answered and nothing passed. `score()` gates hard: credited artist (`album_merge.artist_ok`), album name equal or differing only by edition words (`edition_fit`), live/remix/demo/compilation/soundtrack types, single vs album shape, track count, positions that exist, and the share of local songs matched by ISRC or title (≥ 0.75; all of them, in position, for albums of ≤ 2 songs). A passing release gets a weighted confidence (overlap 0.35, count fit 0.25, position fit 0.20, title fit 0.10, search score 0.10) and is accepted at 0.80. Results are kept permanently in the `album_info` table (`models.AlbumInfo`, one row per album key: release facts, `tracks_json`, `genres_json`, server-side `cover_url`, `confidence`, `pinned`, `rejected_json`); `not_found` is retried after 7 days, `error` after 1 h, and a found non-pinned release that no longer fits the songs (`mismatched()`) is re-checked. "Not this album" (`reject`) records `source:id` (and the MusicBrainz release group) as rejected, clears the row and its cached cover and looks again; Pin takes a pasted MusicBrainz release or Deezer album URL (`parse_release_url`) at confidence 1.0 and survives refreshes. A pin or reject that arrives during a lookup runs next, and the upsert is guarded on `rejected_json` so a lookup never undoes a reject committed meanwhile. `ALBUM_INFO_LOOKUPS=false` turns lookups off.

The overlay rule: nothing in `album_info` / `album_view` writes Track rows, tags, sidecars or file names -- release facts are only displayed. The only writes are the rows an explicit Download creates. `services/album_merge.py` (pure) places library rows on the release tracklist (ISRC, then title within 10 s, then position within 5 s and similarity ≥ 0.8; variant words such as live/remix/inst never match across), and rows the release lacks follow their disc as bonus rows. `services/album_view.py` builds the view; the no-duplicate rule: a release track no pass placed is offered for download only when `library_match.song_index().find_song()` finds that song under no album at all, and the same song under another album shows as `elsewhere` with a link. Downloads need a found, non-mismatched release; "Download missing" also needs confidence ≥ 0.90 or a pin (and more than 2 local songs, or a known track total). The new row takes the group's album/artist names and numbering that keeps the album in one card folder; the client sends only the format.

Routes (`routers/albums.py`, prefix `/api/albums`, keys validated against `a-[0-9a-f]{20}|loose`): `GET ""` and `GET /html` (the Library's Albums grid, `partials/library_albums.html`; `q`, `sort` artist|album|year|recent|size, `offset`/`limit` ≤ 500, loose group last); `GET /{key}` and `GET /{key}/html` (`partials/album_view.html`, DB only: no network, no queueing); `GET /{key}/cover`; `POST /{key}/info/ensure` (202 queued / 204); `GET /{key}/info/status` (204 while a lookup runs, 286 "Still looking" after 90 s, 200 with `HX-Trigger: {"album-info:ready": {"key": ...}}` once settled -- the view and the grid's cover re-request on it); `POST /{key}/info/refresh|reject|pin` (re-render the view); `POST /{key}/download/{disc}/{position}` and `POST /{key}/download-missing` (form `format`). `routers/music.py` opens this view for `provider == "library"` and for the Spotify/YouTube fallbacks below. Cover: `GET /api/albums/{key}/cover` serves the hinted song's (`?t=<track_id>`) sidecar, else any done song's sidecar, else the release cover `_cache_cover` downloaded once into `data/album-covers/<key>.jpg` (only for albums none of whose songs has a sidecar; deleted on reject and prune); the release cover never goes through `/api/art/proxy`, which is only the last resort (a 307) for a track's own `artwork_url`. `python -m scripts.enrich_albums` prefetches the whole library: a read-only dry run by default (DB opened read-only, no requests); `--apply [--limit N] [--force] [--key KEY] [--source musicbrainz|deezer|any] [--prune]` runs MusicBrainz at 1.5 s and holds `data/album_info.lock` (trusted while under 180 s old) so the server queues no lookups meanwhile.

### Spotify album fallback and rate-limit hygiene
`services/spotify_albums.get_album()` caches album tracklists in `data/spotify_albums/<id>.json` (fresh for 30 days, and only while the entry has ISRCs; a stale entry is served whenever Spotify is rate-limiting or failing). The album detail (`routers/music._album_detail_response`) then falls back to the same album on YouTube Music (`find_youtube_album_by_name`, the Spotify name kept as an album alias for matching), then to the library's copy (`local_albums.find_key` → album view) with a notice, then "unavailable". `services/library_match.py` (`LibraryIndex`) matches provider tracks to library rows by Spotify URI, YouTube id, ISRC, or edition-free title + primary artist + album (deluxe/remaster editions count), so a fallback view never offers a second copy. Every Spotify request goes through `_GuardedSpotify._internal_call`: nothing is sent while rate-limited and every 429 starts the window (`Retry-After`, at least 60 s); `get_client()` uses a plain `requests.Session` because spotipy's retry adapter would sleep through an hours-long `Retry-After`, and `@_web_api(default)` returns the default while limited. The window is persisted in `data/spotify_rate_limit.json` (`{"until": <epoch>}`) so a restart never calls into it; `rate_limit_message()` explains the limit on the Home card, search and the Music tabs. `SyncPacer` spaces bulk library reads (3 s plus up to 1.5 s jitter, at most 8 calls per 30 s).

### Rockbox cards and protected card paths
`services/device.py` protects card-root entries that belong to the player firmware or the OS -- `.rockbox`, `System Volume Information`, `$RECYCLE.BIN`, `.Trashes`, `.Spotlight-V100`, `.fseventsd`, `.Trash-<n>`, case-insensitively, and everything beneath them: never listed, imported, written, swept or deleted. Every card deletion checks `is_protected_card_path` / `is_protected_on_card` first (both also refuse `..`, paths outside the root and links resolving into a protected entry); an artist named like one gets a `_` folder prefix. `build_device_path()` keeps paths within Rockbox's MAX_PATH: `"/" + path` ≤ 250 UTF-8 bytes, artist folder ≤ 200, artist + album ≤ 210 (capped independently of the title so an album stays in one folder), then only the title is trimmed, with a `~` + 6-hex SHA-256 tag so long names stay apart; every path of up to 209 bytes is unchanged. `card_profile(root)` is `rockbox` when the root holds a `.rockbox` folder, else `stock`; `detect_devices()` reports it as `profile`.

On a Rockbox card the sync writes playlists to `/Playlists` (`#EXTM3U`, UTF-8 without BOM, LF, entries absolute from the card root; the manifest records `Playlists/<name>`), never overwrites a file there the manifest does not list (`ForeignPlaylistError`), and moves Leasure's playlists between the root and `/Playlists` when the profile changes. `services/covers.py` writes a baseline `cover.jpg` (≤ 500 px) per album folder from the album's sidecar and records it in `/.leasure-covers.json` (size, mtime, SHA-256); a `cover.jpg` Leasure did not write, or one changed on the card, is never touched, and `sweep_album_covers()` (any profile) removes only recorded, unchanged covers of albums gone from the library. Rockbox cannot decode progressive JPEG, so a progressive or oversized `.jpg` sidecar reaches the card as a baseline copy cached in `data/card-cache/covers/<sha256>-<size>.jpg` (`artwork.rockbox_sidecar`); a track sidecar that would collide with `cover.jpg` is left out. Sync never modifies library files. The sync preview carries `profile`, `playlist_dir`, `db_reminder` and, on Rockbox only, `fs_warning` when `card_filesystem()` (Win32 `GetVolumeInformationW`; PowerShell on WSL2, slow, so thread-only and only when needed; `/proc/mounts` on Linux) knows the card is not FAT; the `done` event carries `reminder` (`ROCKBOX_DB_REMINDER`: Settings → General Settings → Database → Initialize Now, or Auto Update). The 4 GiB file limit applies only on FAT32 (or a Rockbox card whose filesystem cannot be read), and the filesystem is asked only for a file that big. Card import re-encodes progressive card sidecars as baseline into the library and also reads `/Playlists` on a Rockbox card.

### Saved libraries (step 1: readers only)
`services/qobuz_client.py` reads the user's Qobuz library: `favorite_ids()`, `favorites(kind)` / `iter_favorites` (tracks | albums | artists), `user_playlists` / `iter_user_playlists`, `playlist` / `iter_playlist_tracks`, `album_page` / `get_album` (offset paging while `total` is larger); pages of 500, stepping down to 100 after one 400, `LIBRARY_PAGE_CAP` 50 pages; an unknown answer shape raises `QobuzShapeError`. Signing is per endpoint: `_library_get` sends the first call to each endpoint unsigned and retries a 400/401/403 once signed (`library_signature` = md5(object + method + ts + secret) with no parameters, as qobuz-dl signs `favorite/getUserFavorites`; unverified for the other endpoints) with the secret getFileUrl proved; the mode that worked is remembered per endpoint and stored as `favorites_signed` in `data/qobuz_app.json`. `services/spotify_client.py` adds `saved_tracks_page`, `saved_albums_page`, `user_playlists_page`, `playlist_items_page`, `followed_artists_page` (cursor-paged) and `fetch_*` readers returning a `SavedListing` (`complete`, `resume`, `stopped`: rate_limited | page_cap | unreadable | scope_missing | not_connected) under `PAGE_CAPS` (tracks 60, albums / playlists / artists 20, playlist items 50) with an optional `pace` (a `SyncPacer`). `user-follow-read` is requested on (re)connect (`AUTH_SCOPES`) but not required: `get_client()` validates against `SCOPES` so older tokens keep working, and `follow_scope_status()` probes whether it was granted. Checks: `python -m scripts.qobuz_check --favorites` (about 12 calls, no search/stream/CDN, counts and field names only, verdict in `data/qobuz_favorites_check.json`) and `python -m scripts.spotify_saved_check` (about 9 calls ≥ 3 s apart, refuses to start while rate-limited, stops at the first 429); both exit 0 / 1 / 2 (2 = not signed in / connected). Not built yet: the Saved tab itself (design draft at `data/designs/saved-libraries.md`, gitignored). Decided defaults: Best = Qobuz FLAC, songs Qobuz lacks as MP3 320 from YouTube, never AAC; the Qobuz daily cap splits 60 bulk / 20 manual / 20 upgrades; a batch is refused when it would leave less than 20 GB free.

## File Structure

```
app.py              -- FastAPI app entry point, lifespan, page routes
config.py           -- Pydantic Settings (reads .env)
db.py               -- SQLAlchemy async engine, session factory
models.py           -- ORM models: Track, Playlist, PlaylistTrack, SyncHistory, CardCopy, SupersededFile, AlbumInfo
worker.py           -- Async download queue (priority queue: downloads before FLAC upgrades; deferral scheduler)

routers/
  spotify.py        -- Spotify browse + download (htmx + JSON endpoints)
  youtube.py        -- YouTube Music browse + download
  device.py         -- Device detection, sync (SSE stream), diff, file browser
  downloads.py      -- Download queue status
  library.py        -- Library browsing
  qobuz.py          -- Qobuz card (sign-in, check) and FLAC upgrade / revert endpoints
  art.py            -- Same-origin cover-art proxy (allow-listed Spotify/YouTube/Qobuz image hosts)
  albums.py         -- Library Albums grid, local album view, album-info lookup/pin/reject, missing-song downloads

services/
  browser_session.py -- Reads the YouTube login from Leasure's own browser window over CDP
  spotify_client.py -- Spotipy OAuth wrapper, rate-limit guard (persisted window), SyncPacer, saved-library readers
  spotify_albums.py -- Spotify album tracklist cache (data/spotify_albums/, 30 days, stale served while limited)
  library_match.py  -- Which library row a provider track already is (URI, YouTube id, ISRC, title+artist+album)
  local_albums.py   -- Library album groups: frozen album keys, loose songs, scan cache, provider-album lookup
  album_info.py     -- Background album-info job (MusicBrainz, then Deezer), gates/confidence, album_info table
  album_merge.py    -- Pure matching: edition fit, artist check, merge of library rows onto a release tracklist
  album_view.py     -- Album view context (merged rows, no-duplicate rule) and the downloads it offers
  deezer.py         -- Deezer public catalogue API (album search, facts, tracklists), throttled
  covers.py         -- Rockbox per-album cover.jpg (baseline) + /.leasure-covers.json manifest
  youtube_client.py -- ytmusicapi wrapper with Chrome cookie auto-refresh
  downloader.py     -- Routes by requested format: Qobuz first for Best/FLAC-only, yt_engine otherwise
  yt_engine.py      -- The YouTube pipeline: scored match, staging dir, provenance, atomic move
  ytdlp_opts.py     -- Shared yt-dlp option sets (cookie file, throttle, PO-token solver)
  qobuz_client.py   -- Direct Qobuz API client (sync httpx): app id/secret scrape, signing, CDN download, log redaction,
                       favorites / playlists / album paging
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
  device.py         -- Device detection (per-platform backends), FAT32 filename sanitization, device path builder,
                       protected card paths, card profile (rockbox | stock), card filesystem
  wsl_mount.py      -- WSL2 passwordless card mount/eject via `wsl.exe -u root` (sudo -n fallback)
  card_import.py    -- Rebuilds the library from the songs on an H2 card (card is only read)
  playlist.py       -- M3U playlist generation (H2 root / Rockbox /Playlists) + on-card manifest of generated files
  oauth_state.py    -- one-shot OAuth state tokens shared by Spotify and Google flows

scripts/
  import_card.py    -- Import the songs on an H2 card into the library (`--source`, `--dry-run`; report in data/card-import/)
  move_library.py   -- Move the library to DEST with size+SHA-256 checks, rewrite Track.file_path (`--dry-run`, `--force`)
  clean_card.py     -- Delete the card copies an import made stale, after a sync; dry run unless `--apply`
  qobuz_check.py    -- Redacted report of what Qobuz answers for the stored sign-in (`--probe-cdn`, `--favorites`; exit 0/1/2)
  spotify_saved_check.py -- What Spotify answers for the saved-library reads (counts/statuses only; exit 0/1/2)
  enrich_albums.py  -- Prefetch album info for every library album; dry run unless `--apply` (`--prune`, `--source`)

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
