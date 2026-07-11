#!/usr/bin/env bash
# Start the Leasure server (Linux / WSL2).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -x .venv/bin/python ]; then
    echo "Error: .venv not found. Run scripts/install.sh first." >&2
    exit 1
fi

exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8642
