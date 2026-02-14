#!/usr/bin/env bash
set -euo pipefail

pids=$(ps aux | rg -n "tg_personal_bot.py" | awk '{print $2}')
if [[ -z "${pids:-}" ]]; then
  echo "No tg_personal_bot.py process found."
  exit 0
fi

echo "Stopping PIDs: $pids"
kill $pids
