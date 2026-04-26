#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

TS=$(date -u +%Y%m%d_%H%M%S)
REPORT_ROOT="reports/mid"
OUT="${REPORT_ROOT}/${TS}"
STAGING="${REPORT_ROOT}/.tmp_${TS}"
PROMPT_FILE="prompt_mid.txt"
TMP_OUT="/tmp/mid_${TS}.out"
MARKER="/tmp/mid_${TS}.marker"

mkdir -p "$REPORT_ROOT"
rm -rf "$STAGING"
mkdir -p "$STAGING"
: > "$MARKER"

. /tmp/inject_prev_mid.sh
cp -f "$PROMPT_FILE" "$STAGING/_prompt_used.txt" || true

# обновляем event calendar перед MID; при сбое оставляем последний валидный файл
python3 tools/update_event_calendar.py >"/tmp/event_calendar_mid_${TS}.out" 2>&1 || true

run_status=0
if EVENT_CALENDAR_PROFILE=mid DISABLE_STATUS_SNAPSHOT=1 SIGNAL_SKIP_AIA_SEND=1 \
    ./signal full --analysis-prompt "$PROMPT_FILE" >"$TMP_OUT" 2>&1; then
  run_status=0
else
  run_status=$?
fi

cp -f "$TMP_OUT" "$STAGING/run.log" 2>/dev/null || true

analysis_path=$(find . -maxdepth 1 -type f -name 'analysis_*.md' -newer "$MARKER" | sort | tail -n1 || true)
run_last_json=$(find logs -maxdepth 1 -type f -name 'last_[0-9]*.json' ! -name '*.raw.json' ! -name '*.clean.json' -newer "$MARKER" | sort | tail -n1 || true)
run_last_raw=$(find logs -maxdepth 1 -type f -name 'last_[0-9]*.raw.json' -newer "$MARKER" | sort | tail -n1 || true)
run_last_clean=$(find logs -maxdepth 1 -type f -name 'last_[0-9]*.clean.json' -newer "$MARKER" | sort | tail -n1 || true)
run_signal_log=$(find logs -maxdepth 1 -type f -name 'signal_*.log' -newer "$MARKER" | sort | tail -n1 || true)
run_signal_html=$(find . -maxdepth 1 -type f -name 'signal_*.html' -newer "$MARKER" | sort | tail -n1 || true)

if [ "$run_status" -ne 0 ] || [ -z "${analysis_path:-}" ] || [ -z "${run_last_json:-}" ]; then
  rm -rf "$STAGING"
  rm -f "$MARKER"
  echo "❌ MID report failed; no fresh MID artifacts created" >&2
  exit 1
fi

cp -f "$analysis_path" "$STAGING"/
if [ -n "$OUT" ] && [ -d "$STAGING" ]; then
  for f in "$STAGING"/analysis_*.md; do
    [ -f "$f" ] || continue
    sed -i '/^=== \[SNAPSHOT ДЛЯ LLM] ===$/,/^==========================$/d' "$f" || true
  done
fi

cp -f "$run_last_json" "$STAGING/last.json"
[ -n "${run_last_raw:-}" ] && cp -f "$run_last_raw" "$STAGING"/
[ -n "${run_last_clean:-}" ] && cp -f "$run_last_clean" "$STAGING"/
[ -n "${run_signal_log:-}" ] && cp -f "$run_signal_log" "$STAGING"/
[ -n "${run_signal_html:-}" ] && cp -f "$run_signal_html" "$STAGING"/

if [ -f "$analysis_path" ]; then
  sed -i '/^=== \[SNAPSHOT ДЛЯ LLM] ===$/,/^==========================$/d' "$analysis_path" || true
fi

mv "$STAGING" "$OUT"
rm -f "$MARKER"

echo "✅ MID report: $OUT"
