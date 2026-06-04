#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${SIGNAL_PYTHON_BIN:-./.venv/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
exec "$PYTHON_BIN" scheduled_runner.py --job day
