#!/usr/bin/env bash
# Start the Leasure server (Linux / WSL2).
# Usage: scripts/run.sh [port]   (default 8642)
set -euo pipefail

PORT="${1:-8642}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${LEASURE_VENV:-.venv}"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Error: $VENV_DIR not found. Run scripts/install.sh first (set LEASURE_VENV to use a venv elsewhere)." >&2
    exit 1
fi

exec "$VENV_DIR/bin/python" -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
