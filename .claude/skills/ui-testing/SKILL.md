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

```bash
curl -sf http://127.0.0.1:8642/ >/dev/null || (cd <repo> && LEASURE_VENV=~/.venvs/leasure nohup scripts/run.sh 8642 >/tmp/leasure-server.log 2>&1 &)
timeout 30 bash -c 'until curl -sf http://127.0.0.1:8642/ >/dev/null; do sleep 1; done'
```

Never `pkill -f uvicorn` with a broad pattern (it can match your own shell). To restart, kill the port listener: `lsof -ti:8642 -sTCP:LISTEN | xargs -r kill`.

The app on this box has **no Spotify/YouTube credentials, no library and no H2 attached**; test the states that exist (empty library, "not configured" cards, device scan showing only the system drive, sync refusal) and the flows that can be exercised with seeded data (see below). Do not try to log in to Spotify or YouTube.

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
    page.fill("#device-path", "/mnt/c")
    page.click("#sync-btn")
    page.wait_for_selector(".sync-result-error", timeout=10000)
    shot("sync-refused")
    return {"error_text": page.locator(".sync-result-error").inner_text()}
```

Always `Read` the screenshots you take and describe what you actually see. A blank or half-painted frame is a finding.

## Seeding state without providers

The API is local and unauthenticated. To make a non-empty library for a scenario, insert rows into the SQLite DB the server uses (`data/leasure.db` in the repo) with `sqlite3` or a small Python snippet using `sqlalchemy` from `~/.venvs/leasure` — `tracks(status='done', file_path=...)` rows appear in the library, footer stats and carousel. Point `file_path` at real files you create under a temp dir. Remove what you inserted when done (delete by a recognisable title prefix such as `UITEST `).

## What a finding looks like

Concrete and reproducible: the panel, the steps, what a user expected, what happened, the screenshot path, and any console/network error text. Rate severity: **blocker** (cannot complete a core flow), **major** (wrong result or misleading state), **minor** (cosmetic/inconsistency), **note** (suggestion).
