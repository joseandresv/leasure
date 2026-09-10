---
name: python-craft
description: Python peculiarities for Leasure's backend — FastAPI + asyncio event-loop discipline, async SQLAlchemy 2.0 with SQLite, pydantic-settings, pathlib across Windows/WSL2/Linux, mutagen tag writers, yt-dlp/ytmusicapi/spotipy behaviour, datetime/timezone, testing with pytest-asyncio. Load before editing any .py file.
---

# Python craft for Leasure

Python 3.11+ (3.14 in the dev venv). Verify library behaviour against the *installed*
version in the venv (`python -c "import x; print(x.__version__)"`, read
`site-packages`), never from memory — yt-dlp and ytmusicapi change monthly.

## 1. The event loop is the whole app

- Every `async def` handler shares one loop. A synchronous call that blocks (spotipy,
  ytmusicapi, `yt_dlp.YoutubeDL`, `subprocess.run`, `httpx.Client`, `Path.rglob`,
  `shutil.copyfile`, `mutagen` saves, `PIL`) stalls every other request. Wrap it:
  `await asyncio.to_thread(fn, *args)`. Pass the function and args, not a lambda that
  closes over ORM objects from another session.
- Long jobs (downloads, device sync) run as detached `asyncio.Task`s owned by a
  module-level registry, publish progress to an `asyncio.Queue`, and are never awaited
  from inside a request. A client disconnect cancels the *handler*, not the task.
- `CancelledError` is a `BaseException`; `except Exception` does not catch it. Cleanup
  that must survive cancellation goes in `finally` with `asyncio.shield`, or in a fresh
  session opened *inside* the `finally`.
- Never call `asyncio.run()` or `loop.run_until_complete()` inside the app.

## 2. Async SQLAlchemy 2.0 + SQLite

- `async_session()` per unit of work; `expire_on_commit=False` is set, so attributes
  stay readable after commit, but **writes need a live session**: re-`session.get()`
  the row inside a new session before mutating it.
- Use `select(...)` + `session.execute()`/`session.scalar()`; no legacy `session.query`.
- SQLite: one writer at a time. Keep write transactions short; commit in batches inside
  long loops (every ~25 rows) rather than once per row or once at the end.
- Pragmas are set per connection in `db.py` (`foreign_keys=ON`, WAL, busy timeout).
  FK cascades only work because of that pragma.
- Schema changes are additive: add the column to `models.py`; `_migrate_missing_columns`
  adds it on startup after a backup. Non-nullable columns need a constant default.
- `func.now()` server defaults cannot be added to existing rows by `ALTER TABLE`; keep
  such columns nullable.
- Uniqueness: `Track.spotify_uri` is unique; `youtube_id` is not — look up with
  `scalar_one_or_none()` and expect duplicates in old data.

## 3. Settings, paths and platforms

- `config.settings` is a pydantic-settings singleton loaded at import; tests set
  `DATA_DIR`/`LIBRARY_DIR`/`DOWNLOAD_DIR` env vars *before* importing anything.
  Import-time side effects (directory creation, engine construction) are why order matters.
- `pathlib.Path` everywhere; build device paths with `services.device.build_device_path`
  and compare with `.as_posix()`. On native Windows `relative_to()` yields backslashes.
- `os.path.realpath` + `os.path.normcase` before comparing user-supplied paths with
  detected volumes (`resolve_sync_target`). Never write under a path that did not pass it.
- drvfs / FAT32: `shutil.copyfile`, not `copy2`; no `chmod`; filenames through
  `sanitize_filename` (forbidden chars, control chars, reserved names, length).
- `subprocess.run([...], capture_output=True, text=True, timeout=...)` with a list, never
  a shell string; `sudo -n` so it fails instead of prompting.

## 4. Time

- The DB stores naive UTC (`datetime.utcnow()`, deprecated since 3.12). When touching
  such code, write `datetime.now(UTC).replace(tzinfo=None)` for storage and convert for
  display with `.replace(tzinfo=UTC).astimezone()`. Do not mix aware and naive values
  in comparisons.
- Ruff `UP017` wants `datetime.UTC`, not `timezone.utc`.

## 5. Tagging and media libraries

- mutagen: MP3 → `ID3` frames (`TIT2`, `TPE1`, `TALB`, `TPE2`, `TRCK`, `TPOS`, `TCON`,
  `TDRC`, `APIC`); FLAC/Opus/Ogg → Vorbis comments (`title`, `artist`, `albumartist`,
  `tracknumber`, `genre`, `date`, pictures via `metadata_block_picture` base64 on Opus);
  M4A → `MP4` atoms (`\xa9nam`, `\xa9ART`, `\xa9alb`, `aART`, `trkn` as `[(n, total)]`,
  `\xa9gen`, `\xa9day`, `covr` with `MP4Cover`). `add_tags()` raises if tags exist —
  check `audio.tags is None` first.
- Only `primary_genre()` reaches a file; `Track.genre` keeps the comma list.
- yt-dlp: options are plain dicts; `postprocessors=[{"key": "FFmpegExtractAudio",
  "preferredcodec": "best"}]` is a remux (no re-encode); `cookiefile` beats
  `cookiesfrombrowser` when `data/cookies.txt` exists; `remote_components: ["ejs:github"]`
  needs deno on PATH. Build attempts with `services.ytdlp_opts.build_download_attempts`.
- ytmusicapi album tracks carry `trackNumber`; search results carry `videoId`,
  `duration_seconds`. spotipy raises on failed refresh — `get_client()` swallows it.
- HTTP calls that fetch user/provider URLs (artwork) must check the scheme is http(s).

## 6. FastAPI conventions here

- Mutating endpoints are `POST`; `SameOriginMiddleware` rejects cross-site ones. No
  state-changing `GET`.
- HTML from Python goes through `markupsafe.escape`; prefer a Jinja partial with
  `templates.TemplateResponse(request=request, name=..., context=...)`.
- Validation failures → `HTTPException(400, detail=...)` with a sentence a user can act
  on; not-found → 404; never a bare 500 for a provider outage — log and return the
  "not connected" state.
- Depend on `get_session` for request-scoped sessions; open `async_session()` yourself
  only in background tasks.
- SSE: `StreamingResponse(gen(), media_type="text/event-stream")`, each event
  `data: <json>\n\n`; the generator only drains a queue.

## 7. Tests

- pytest-asyncio in auto mode; fixtures `db`, `client` (httpx `ASGITransport`, no
  lifespan, so call `init_db()` via the fixture), `fake_device` (monkeypatched
  `detect_devices`). Seed via the ORM, not raw SQL.
- Monkeypatch provider functions at the module the router imported them from
  (`routers.spotify.sp.is_connected`), not at the definition site only.
- A behaviour change gets a test that fails before and passes after; prove it with
  `git stash` if unsure.
- Windows-only code (`ctypes.windll`) must be guarded so the suite runs on Linux.

## 8. Style

- Ruff config in `pyproject.toml` (E, F, I, B, UP; line length 130). Run `ruff check .`.
- `X | None`, f-strings, `pathlib`, early returns, small functions; logging via the
  module `logger` with `%s` formatting; no print.
