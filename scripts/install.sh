#!/usr/bin/env bash
# Leasure install script (Linux / WSL2).
# Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Where the virtual environment lives. Default is .venv inside the repo; set
# LEASURE_VENV to put it elsewhere (recommended when the checkout sits in a
# OneDrive/Dropbox folder or on a Windows drive under WSL2, where thousands of
# small files are slow to sync and can hit drvfs EIO errors).
VENV_DIR="${LEASURE_VENV:-.venv}"

# --- Python >= 3.11 ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 not found on PATH. Install Python 3.11 or newer." >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Error: Python 3.11 or newer is required, but found $(python3 --version)." >&2
    exit 1
fi
echo "Found $(python3 --version)"

# --- Virtual environment ---
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    echo "Virtual environment $VENV_DIR already exists."
fi

echo "Installing dependencies from requirements.txt ..."
"$VENV_DIR/bin/pip" install -r requirements.txt
if [ "${LEASURE_DEV:-0}" = "1" ]; then
    echo "Installing dev tools (pytest, ruff) ..."
    "$VENV_DIR/bin/pip" install -r requirements-dev.txt
fi

# --- ffmpeg ---
if command -v ffmpeg >/dev/null 2>&1; then
    echo "Found ffmpeg."
else
    echo "Warning: ffmpeg not found on PATH. Install it with:"
    echo "  sudo apt install ffmpeg    (Debian/Ubuntu)"
    echo "  sudo dnf install ffmpeg    (Fedora)"
fi

# --- deno (needed for yt-dlp PO tokens) ---
if command -v deno >/dev/null 2>&1 || [ -x "$HOME/.deno/bin/deno" ]; then
    echo "Found deno."
else
    echo "deno not found (needed for yt-dlp PO tokens)."
    if [ -t 0 ]; then
        read -r -p "Install deno now via https://deno.land/install.sh? [y/N] " answer
        case "$answer" in
            [yY]|[yY][eE][sS])
                curl -fsSL https://deno.land/install.sh | sh
                ;;
            *)
                echo "Skipping deno install. Install it later with:"
                echo "  curl -fsSL https://deno.land/install.sh | sh"
                ;;
        esac
    else
        echo "Non-interactive shell; skipping deno install. Install it later with:"
        echo "  curl -fsSL https://deno.land/install.sh | sh"
    fi
fi

# --- .env ---
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example -- fill in your Spotify credentials."
else
    echo ".env already exists."
fi

echo
echo "Done. Next steps:"
echo "  1. Edit .env and fill in your Spotify credentials."
echo "  2. Start the server: scripts/run.sh"
if [ "$VENV_DIR" != ".venv" ]; then
    echo "     (export LEASURE_VENV=$VENV_DIR first, or run: LEASURE_VENV=$VENV_DIR scripts/run.sh)"
fi
