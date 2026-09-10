---
name: shell-scripts
description: Conventions and pitfalls for Leasure's install/run scripts — Bash (Linux/WSL2) and PowerShell (native Windows) — plus the CI workflow and pyproject/requirements files. Load before editing scripts/, .github/workflows, pyproject.toml or requirements*.txt.
---

# Shell, PowerShell and config files

Two script pairs must stay behaviourally identical: `scripts/install.sh` / `install.ps1`
and `scripts/run.sh` / `run.ps1`. A change to one side (a new env var, a new step, a
new message) is done on the other side in the same task, or reported as a gap.

## 1. Bash (`scripts/*.sh`)

- Header: `#!/usr/bin/env bash` + `set -euo pipefail`. Resolve the repo root from the
  script (`REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`) and `cd` there.
- Quote every expansion; use `[ ]` tests with `-d`/`-x`/`-f`; `command -v` to detect
  tools, never `which`.
- Env-var overrides with defaults: `VENV_DIR="${LEASURE_VENV:-.venv}"`. Document each
  override in the script header and README.
- Interactive prompts only when `[ -t 0 ]`; otherwise print the command the user can run
  and continue. `sudo -n` when a step needs root, so it fails instead of hanging.
- `exec` the final long-running process (uvicorn) so signals reach it.
- Never `pkill -f <broad pattern>`; to free a port use
  `lsof -ti:PORT -sTCP:LISTEN | xargs -r kill`.
- WSL2: files under `/mnt/c` are drvfs (slow, no chmod, OneDrive EIO risk) — keep venvs
  and caches in `$HOME`; the scripts honour `LEASURE_VENV` for that reason.
- Test with `bash -n script.sh` (syntax) and `shellcheck` if available; then actually
  run it in a throwaway directory.

## 2. PowerShell (`scripts/*.ps1`)

- `$ErrorActionPreference = "Stop"`; `$RepoRoot = Split-Path -Parent $PSScriptRoot`.
- Native commands do not throw: check `$LASTEXITCODE` after `& python ...`.
- Prefer `py -3` then `python` when locating the interpreter; run the version check
  through the interpreter itself, not by parsing `--version`.
- Paths: `Join-Path`, never string concatenation with `\`; venv scripts live in
  `.venv\Scripts\`, not `bin/`.
- Execution policy: scripts may need `Set-ExecutionPolicy -Scope Process Bypass`; say
  so in a comment at the top, do not try to change it from the script.
- Environment overrides use `$env:LEASURE_VENV`; mirror every Bash override.
- Chrome cookie extraction on native Windows may fail (app-bound encryption); the
  scripts and README point to `COOKIE_BROWSER=firefox`.
- You cannot run `.ps1` on this Linux box; validate by reading carefully and, where the
  logic is shared, by keeping it structurally parallel to the `.sh` file.

## 3. `pyproject.toml` and requirements

- Runtime deps live in both `pyproject.toml` `[project.dependencies]` and
  `requirements.txt` (the install scripts use the latter). Change both.
- Dev tools go to `requirements-dev.txt` and `[project.optional-dependencies].dev`.
- Ruff and pytest config live in `pyproject.toml`; keep `line-length = 130`,
  `asyncio_mode = "auto"`.
- Pin new packages with a minimum version that has the API you use; check the installed
  version in the venv.

## 4. CI (`.github/workflows/ci.yml`)

- Ubuntu, Python 3.11 and 3.13, `apt-get install ffmpeg` (the M4A test needs it),
  `pip install -r requirements.txt -r requirements-dev.txt`, `ruff check .`, `pytest -q`.
- A test that needs a browser or network must skip cleanly in CI
  (`pytest.mark.skipif`), not fail.
- Keep the workflow YAML minimal; pin actions to a major version (`actions/checkout@v4`).

## 5. `.env` and secrets

- `.env.example` documents every setting in `config.py`; add new settings to both, with
  the default shown. Never commit `.env`, `data/`, `library/`, cookies, or the
  `docs/review-2026-08/` folder (gitignored on purpose).
- `.gitignore` allows only `.claude/skills/` and `.claude/agents/` from `.claude/`.
