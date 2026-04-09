# Leasure

A local music downloader and library manager built for the **HIFI WALKER H2** portable music player. Browse your Spotify and YouTube Music libraries through a web UI, download tracks via YouTube Music (with optional Premium quality), and sync everything to your H2's SD card with proper metadata, album art, synced lyrics, and playlists.

## Features

- **Spotify library browsing** -- saved albums, playlists, liked songs via Spotify Web API (spotipy)
- **YouTube Music library browsing** -- playlists, albums, liked songs via ytmusicapi
- **YouTube Music Premium quality** -- automatic Chrome cookie extraction for higher bitrate downloads via yt-dlp
- **MusicBrainz genre lookup** -- fetches artist genres from MusicBrainz when Spotify's album genre endpoint returns empty (which it usually does)
- **Synced lyrics** -- fetches time-synced `.lrc` lyrics from lrclib.net and saves them as sidecar files
- **H2-compatible metadata** -- writes proper ID3v2.4 tags (MP3) and Vorbis comments (FLAC), embeds album art, and creates sidecar `.jpg` and `.lrc` files that the H2 reads natively
- **Device sync with progress** -- detects mounted drives via WSL2 `/mnt/` paths, copies music with SSE progress streaming, generates `.m3u` playlists at the SD card root
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
| Audio download | yt-dlp (with optional deno for Premium PO token) |
| Audio tagging | mutagen (ID3v2.4 for MP3, Vorbis for FLAC) |
| Artwork processing | Pillow (resize to 500x500 JPEG) |
| Lyrics | lrclib.net REST API |
| Genre lookup | MusicBrainz REST API |
| HTTP client | httpx (async) |

### How downloads work

1. User selects a track/album/playlist from the Spotify or YouTube Music browser
2. Track metadata is saved to SQLite and enqueued in the async download worker
3. The worker searches YouTube Music (ytmusicapi) for the best audio match by title, artist, and duration
4. yt-dlp downloads the audio, trying Chrome cookies first (Premium quality), falling back to standard
5. ffmpeg post-processes to MP3 320kbps or FLAC
6. The tagger service applies full ID3 tags, embeds album art, creates sidecar `.jpg`, and fetches `.lrc` lyrics
7. Genre is looked up from MusicBrainz if not already known

### File organization

Library files are stored as:
```
library/{Artist}/{Album}/{NN} - {Title}.mp3
library/{Artist}/{Album}/{NN} - {Title}.jpg   (sidecar album art)
library/{Artist}/{Album}/{NN} - {Title}.lrc   (synced lyrics)
```

On the H2 SD card, the same structure is used at the root (no `MUSIC/` prefix needed -- the H2 scans the entire card and uses ID3 tags for its Category browser).

## Prerequisites

- **Python 3.11+**
- **ffmpeg** -- required by yt-dlp for audio conversion
- **deno** -- required by yt-dlp for YouTube Premium PO token generation (`curl -fsSL https://deno.land/install.sh | sh`)
- **Chrome browser** -- must be logged into YouTube Music for Premium quality downloads and automatic cookie refresh

## Setup

```bash
# Clone and enter the project
cd leasure

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/WSL

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
```

### Configure Spotify

1. Go to https://developer.spotify.com/dashboard
2. Create a new application
3. Set the redirect URI to `http://localhost:8642/api/spotify/callback`
4. Copy the Client ID and Client Secret into your `.env` file:
   ```
   SPOTIFY_CLIENT_ID=your_client_id_here
   SPOTIFY_CLIENT_SECRET=your_client_secret_here
   ```

### Configure YouTube Music

YouTube Music authentication is handled automatically via Chrome cookies. As long as you are logged into YouTube Music in Chrome, the app will auto-refresh credentials on each connection check.

Alternatively, you can set up manually through the web UI by pasting browser request headers (instructions are shown on the YouTube Music page).

## Usage

```bash
# Activate the virtual environment
source .venv/bin/activate

# Start the server
python app.py
# or: uvicorn app:app --host 127.0.0.1 --port 8642 --reload
```

Open http://127.0.0.1:8642 in your browser.

### Pages

- **Home** (`/`) -- library stats and album art carousel
- **Spotify** (`/spotify`) -- browse and download from your Spotify library
- **YouTube Music** (`/youtube`) -- browse and download from your YouTube Music library
- **Downloads** (`/downloads`) -- monitor download queue and status
- **Library** (`/library`) -- browse your downloaded music collection
- **Device** (`/device`) -- detect your H2, preview sync diff, sync with progress

### Downloading music

1. Navigate to the Spotify or YouTube Music page
2. Connect your account (Spotify via OAuth, YouTube Music via Chrome cookies)
3. Browse your albums, playlists, or liked songs
4. Click download on individual tracks, full albums, or entire playlists
5. Monitor progress on the Downloads page

### Syncing to H2

1. Connect your H2 via USB and ensure it is mounted (WSL2 mounts drives under `/mnt/`)
2. Go to the Device page
3. Select your H2 from the detected devices list
4. Preview the sync diff to see what will be added
5. Click sync -- progress streams in real-time via SSE
6. Playlists are generated as `.m3u` files at the SD card root

## HIFI WALKER H2 Compatibility

The H2 is a portable HiFi music player that reads music from a micro SD card. Leasure is specifically built around its requirements:

### Metadata

- **ID3v2.4 tags** for MP3: TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TCON, TDRC, APIC
- **Vorbis comments** for FLAC: title, artist, album, albumartist, tracknumber, discnumber, genre, date
- **Album artist** (TPE2) is always set -- the H2 uses this for its Category browser; defaults to the primary artist if not explicitly provided

### Sidecar files

- `.jpg` -- album art resized to 500x500, same filename as audio file. The H2 reads these for album art display.
- `.lrc` -- time-synced lyrics in LRC format. The H2 displays these during playback.

### Playlists

- `.m3u` files must be at the **root** of the SD card
- Paths inside are absolute from SD root (e.g., `/Artist/Album/01 - Track.mp3`)
- The H2 shows these under its Explorer, not the Playlists category

### Folder structure

Artist folders go directly at the SD card root. No `MUSIC/` prefix is needed. The H2 scans the entire card and builds its Category view from ID3 tags, not folder structure.

### Filename sanitization

All filenames are sanitized for FAT32 compatibility (no `\/:*?"<>|` characters, max 200 chars).

## Security

- All Python packages in `requirements.txt` are pinned to minimum versions audited for known CVEs
- No credentials are stored in source code; all secrets go in `.env` (excluded from git)
- Spotify OAuth tokens are cached locally in `data/.spotify_cache`
- YouTube Music headers are stored locally in `data/youtube_headers.json`
- The server binds to `127.0.0.1` by default (localhost only)

## Project Structure

```
leasure/
  app.py                  # FastAPI app, lifespan, page routes
  config.py               # Pydantic settings from .env
  db.py                   # SQLAlchemy async engine + session
  models.py               # Track, Playlist, PlaylistTrack, SyncHistory
  worker.py               # Async download queue worker
  requirements.txt        # Pinned dependencies
  .env.example            # Environment template (no secrets)
  routers/
    spotify.py            # Spotify browsing + download endpoints
    youtube.py            # YouTube Music browsing + download endpoints
    device.py             # Device detection, sync, diff
    downloads.py          # Download queue monitoring
    library.py            # Library browsing
  services/
    spotify_client.py     # Spotipy wrapper (OAuth, library access)
    youtube_client.py     # ytmusicapi wrapper (cookie auth, library access)
    downloader.py         # Download dispatcher (routes to engines)
    spotdl_engine.py      # yt-dlp download with YTMusic search + Chrome cookies
    ytdlp_engine.py       # Direct yt-dlp download for YouTube sources
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
