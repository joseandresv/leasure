---
name: ui-testing
description: Drive the Leasure web UI in a headless Chromium (Playwright) like a real user — start the server, click through panels, take screenshots, collect console/network errors. Load before any browser-based evaluation of the app.
---

# UI testing with a headless browser

There is no computer-use tool in this environment. Browser testing is done with
Python Playwright driving a headless Chromium, installed user-side (no sudo).

## Environment (this machine)

```bash
export LD_LIBRARY_PATH=$HOME/.local/chromium-deps/usr/lib/x86_64-linux-gnu   # nss/nspr/asound extracted from .deb, no root
PW=$HOME/.venvs/leasure-tester/bin/python                                     # venv with playwright + chromium (1234)
```

If `$PW -c "import playwright"` fails, recreate: `uv venv ~/.venvs/leasure-tester && uv pip install --python ~/.venvs/leasure-tester/bin/python playwright && ~/.venvs/leasure-tester/bin/python -m playwright install chromium`. If Chromium reports a missing `lib*.so`, `apt-get download <pkg>` into a temp dir, `dpkg-deb -x` it into `~/.local/chromium-deps`, and keep `LD_LIBRARY_PATH` set.

## Server

Never test against the owner's server on **8642** or the real `data/` / `library/` (a stray run once emptied the real DB). Start your own, isolated, on 8650:

```bash
T=$(mktemp -d); mkdir -p "$T"/{data,library,downloads}
(cd <repo> && DATA_DIR="$T/data" LIBRARY_DIR="$T/library" DOWNLOAD_DIR="$T/downloads" DOWNLOAD_WORKER_ENABLED=false \
  LEASURE_VENV=~/.venvs/leasure nohup scripts/run.sh 8650 >"$T/server.log" 2>&1 &)
timeout 30 bash -c 'until curl -sf http://127.0.0.1:8650/ >/dev/null; do sleep 1; done'
lsof -ti:8650 -sTCP:LISTEN >"$T/server.pid"                     # the uvicorn PID itself, not a shell wrapper
grep -q "Download worker disabled" "$T/server.log" || { echo "WORKER RUNNING - stop"; kill "$(cat "$T/server.pid")"; exit 1; }
curl -sf http://127.0.0.1:8650/api/library/stats | grep -q '"total_tracks":0' || { echo "NOT ISOLATED - stop"; kill "$(cat "$T/server.pid")"; exit 1; }
```

If the footer/library does not show 0 tracks, the server is not using the temp dirs: stop and report, do not continue. When done, kill your own uvicorn PID (`kill "$(cat "$T/server.pid")"`) and confirm `lsof -ti:8650 -sTCP:LISTEN` prints nothing — never `pkill -f uvicorn`, never anything listening on 8642.

Hard rules (a tester once re-queued 129 tracks and downloaded into the real library with the owner's cookies):

- Always `DOWNLOAD_WORKER_ENABLED=false`: the worker otherwise re-queues every pending track at startup.
- Never point `LIBRARY_DIR` (or `DATA_DIR`/`DOWNLOAD_DIR`) at the real `library/`; copy only the files a scenario needs into `$T/library`.
- If a scenario needs a copy of the real DB, before starting the server set every non-`done` track to a non-queued status in the copy: `sqlite3 "$T/data/leasure.db" "UPDATE tracks SET status='error' WHERE status<>'done'"`.

Your isolated server has **no Spotify/YouTube login and an empty library**, but it does see the owner's real cards (`/mnt/d`, `/mnt/e`) — look, never mount, eject or sync them. Test the states that exist (empty library, "not configured" cards, device scan, mocked sync) and the flows that can be exercised with seeded data (see below). Do not try to log in to Spotify or YouTube.

## Probe script

`probe.py` in this skill directory runs a scenario, saves numbered screenshots and `report.json` (console errors, uncaught page errors, requests with status ≥ 400 or network failure):

```bash
$PW .claude/skills/ui-testing/probe.py --out /tmp/ui/home --scenario all
$PW .claude/skills/ui-testing/probe.py --out /tmp/ui/custom --script /tmp/ui/steps.py
```

A custom `steps.py`:

```python
def steps(page, shot):
    page.goto(page.base_url, wait_until="networkidle")
    page.locator('#nav-buttons button[data-panel="device"]').click()   # home|music|library|downloads|device
    page.wait_for_timeout(800)
    shot("device-panel")
    page.fill("#device-path", "/tmp/uitest-card")
    page.click("#sync-btn")                                            # mocked by probe.py
    page.wait_for_selector(".sync-result-success", timeout=10000)
    shot("sync-done")
    return {"result_text": page.locator(".sync-result-success").inner_text()}
```

Always `Read` the screenshots you take and describe what you actually see. A blank or half-painted frame is a finding.

## Seeding state without providers

The API is local and unauthenticated. To make a non-empty library, insert rows only into your temp DB, `$T/data/leasure.db` (created by the server at startup), with `sqlite3` — `tracks(status='done', file_path=...)` rows appear in the library, footer stats and carousel. Point `file_path` at files you create under `$T/library`. Never open the repo's `data/leasure.db`. Throwing away `$T` cleans up.

`probe.py` always aborts `POST /api/device/mount|eject|automount|sync` (real cards on `/mnt/d`, `/mnt/e` are visible to the server) and answers `sync/start` / `sync/stream/<id>` with a canned one-track success, so sync UI can be tested without copying anything. Keep those routes in place in custom scripts.

## What a finding looks like

Concrete and reproducible: the panel, the steps, what a user expected, what happened, the screenshot path, and any console/network error text. Rate severity: **blocker** (cannot complete a core flow), **major** (wrong result or misleading state), **minor** (cosmetic/inconsistency), **note** (suggestion).
