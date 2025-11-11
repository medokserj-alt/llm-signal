#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
TS=$(date -u +%Y%m%d_%H%M%S)
OUT="reports/mid/${TS}"
mkdir -p "$OUT"

cp -f prompt_analysis.txt prompt_analysis.bak
cp -f prompt_mid.txt      prompt_analysis.txt

. /tmp/inject_prev_mid.sh
cp -f prompt_analysis.txt "$OUT/_prompt_used.txt" || true
DISABLE_STATUS_SNAPSHOT=1 ./signal full >/tmp/mid_${TS}.out 2>&1 || true

cp -f analysis_*.md "$OUT"/ 2>/dev/null || true
cp -f logs/last.json "$OUT"/ 2>/dev/null || true
cp -f /tmp/mid_${TS}.out "$OUT"/run.log 2>/dev/null || true
mv -f prompt_analysis.bak prompt_analysis.txt
echo "✅ MID report: $OUT"

# --- strip SNAPSHOT block from analysis files in $OUT ---
if [ -n "$OUT" ] && [ -d "$OUT" ]; then
  for f in "$OUT"/analysis_*.md; do
    [ -f "$f" ] || continue
    sed -i '/^=== \[SNAPSHOT ДЛЯ LLM] ===$/,/^==========================$/d' "$f" || true
  done
fi
