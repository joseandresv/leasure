---
name: coding-standards
description: Coding rules for agents that edit the Leasure repo — minimal diffs, clean code, lowest-verbosity comments, verify with ruff + pytest, report precisely. Load before making any code change.
---

# Coding standards for Leasure change agents

You are one of several agents editing this repository. The orchestrator gave you a
scoped task; your job is to complete exactly that task, cleanly, and report back.
Read `CLAUDE.md` at the repo root first for architecture and platform constraints.

## 1. Scope: the smallest correct change

- Change only what the task requires. No drive-by refactors, renames, reformatting,
  import reordering, or "while I'm here" fixes outside the task.
- If you notice a real problem outside scope, put it in your report under
  "Noticed, not changed". Do not fix it.
- Prefer editing an existing function over adding a new one; prefer adding a
  parameter over duplicating a code path.
- Do not add dependencies. If the task cannot be done without one, stop and say so.
- Do not create new files unless the task needs a new module or test file.
- Never delete or rewrite tests to make them pass. Fix the code, or report why the
  test is wrong.
- Do not touch `docs/review-2026-08/`, `.env`, `data/`, `library/`, or git state
  (no commits, no branches, no stashes) unless told to.

## 2. Clean code

- Follow the conventions already in the file you are editing (naming, async style,
  logging via `logger`, error handling). Consistency with neighbours beats personal
  preference.
- Small functions with one job. If a function grows past ~40 lines, split it.
- Names say what a thing is: `resolve_sync_target`, not `check` or `handle`.
- No dead code: remove what you replaced, do not comment it out. No unused imports,
  variables, or parameters.
- Fail loudly at boundaries (HTTP handlers, worker), never swallow exceptions with
  bare `except: pass`. Log with context (`logger.warning("... %s", value)`).
- Blocking I/O (provider SDKs, `subprocess`, `rglob`, `shutil`) never runs directly
  in an `async def` handler — wrap with `asyncio.to_thread`.
- Anything rendered into HTML from provider or user data goes through
  `markupsafe.escape` (server) or `textContent` (JS). Never build HTML from raw strings.
- Paths that reach the SD card go through `sanitize_filename` / `build_device_path`
  and are compared as POSIX (`as_posix()`); `shutil.copyfile`, not `copy2`, on drvfs.
- Python 3.11+ syntax: `X | None`, `match` where it reads better, f-strings, `pathlib`.

## 3. Comments and docstrings: lowest verbosity

- Default is **no comment**. Code should read without one.
- Write a comment only when the *why* is non-obvious: a platform quirk, a
  counter-intuitive ordering, a workaround for an external bug, a security reason.
  One or two lines, above the code, in plain English.
- Never narrate the *what* (`# increment counter`, `# loop over tracks`), never
  restate the function name, never leave TODOs, section banners, or change logs
  (`# added by ...`, `# fixed bug ...`) in the code.
- Docstrings: one line for public functions whose purpose is not obvious from the
  name and signature. Skip them for private helpers and trivial functions. No
  Args/Returns blocks unless a parameter's meaning is genuinely unclear.
- Do not add comments to code you did not otherwise change.
- Do not delete existing comments that still explain a real *why*.

## 4. Tests

- A behaviour change gets a test in `tests/` that fails before and passes after.
  Use the existing fixtures in `tests/conftest.py` (`db`, `client`, `fake_device`).
- Tests are deterministic and offline: no network, no real browser, no real drives.
  Monkeypatch provider functions; use `tmp_path`.
- One assertion idea per test; name says the behaviour
  (`test_sweep_never_touches_user_playlists`).

## 5. Verify before you report

Run from the repo root with the project venv
(`/home/joseandresv-desktop/.venvs/leasure/bin/python` on this machine, otherwise `.venv`):

```bash
ruff check .
python -m pytest -q
```

Both must be clean. If you cannot make them clean, report the failing output verbatim
instead of weakening the check.

For anything touching a route, also confirm the app imports:
`python -c "import app"` (with `DATA_DIR`, `LIBRARY_DIR`, `DOWNLOAD_DIR` pointed at a temp dir).

## 6. Report format (your final message)

Keep it under 300 words, no code dumps:

1. **Done** — one line per change, `file:function` and what it now does.
2. **Verification** — the exact commands run and their one-line result.
3. **Noticed, not changed** — out-of-scope issues, one line each (may be empty).
4. **Open questions** — only if something blocked you or needed an assumption; state
   the assumption you took.

Do not summarise the task back, do not paste diffs, do not explain the reasoning behind
things you did not change.
