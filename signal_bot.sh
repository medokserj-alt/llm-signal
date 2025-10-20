#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Гоним полный прогон и даём боту чистый анализ без служебных строк
LOG_TAIL=0 ./signal full >/tmp/signal.run.out 2>/tmp/signal.run.err || true

# Если в stdout попал текст анализа — вырежем его начиная с первого SNAPSHOT
if grep -q '^=== \[SNAPSHOT' /tmp/signal.run.out; then
  sed -n '/^=== \[SNAPSHOT/,$p' /tmp/signal.run.out
else
  # Резерв: возьмём из последнего run_log
  last_log="$(ls -t logs/signal_*.log | head -n1)"
  if [ -n "${last_log:-}" ] && grep -q '^=== \[SNAPSHOT' "$last_log"; then
    sed -n '/^=== \[SNAPSHOT/,$p' "$last_log"
  else
    echo "[WARN] SNAPSHOT not found in stdout/logs; nothing to send."
  fi
fi
