#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

TS=$(date -u +%Y%m%d_%H%M%S)
OUT="reports/day/${TS}"
mkdir -p "$OUT"

# подменяем промпт на DAY
cp -f prompt_analysis.txt prompt_analysis.bak
cp -f prompt_day.txt      prompt_analysis.txt

# грузим предыдущие DAY/MID для ссылки в промпте
PREV_JSON=$(./load_prev_reports.py 2>/dev/null | tee /tmp/prev_reports.txt || true)
export PREV_DAY="$(grep -A200 '=== LAST DAY ===' /tmp/prev_reports.txt | sed -n '2,400p' | tr -d '\r' || true)"
export PREV_MID="$(grep -A200 '=== LAST MID ===' /tmp/prev_reports.txt | sed -n '2,400p' | tr -d '\r' || true)"

# сохраняем фактически использованный промпт
cp -f prompt_analysis.txt "$OUT/_prompt_used.txt" || true

# обновляем event calendar перед DAY; при сбое оставляем последний валидный файл
python3 tools/update_event_calendar.py >/tmp/event_calendar_day_${TS}.out 2>&1 || true

# генерация
EVENT_CALENDAR_PROFILE=day DISABLE_STATUS_SNAPSHOT=1 SIGNAL_SKIP_AIA_SEND=1 ./signal full >"/tmp/day_${TS}.out" 2>&1 || true

# сбор артефактов
cp -f analysis_*.md      "$OUT"/ 2>/dev/null || true
# — очистка SNAPSHOT в копиях отчёта ($OUT)

for f in "$OUT"/analysis_*.md; do 
  [ -f "$f" ] && sed -i '/^=== \[SNAPSHOT ДЛЯ LLM] ===$/,/^==========================$/d' "$f"; 
done
# cp -f signal_*.html      "$OUT"/ 2>/dev/null || true
cp -f logs/last.json     "$OUT"/ 2>/dev/null || true
cp -f "/tmp/day_${TS}.out" "$OUT"/run.log 2>/dev/null || true

# откат промпта
mv -f prompt_analysis.bak prompt_analysis.txt

# чистим SNAPSHOT из последнего analysis
AN=$(ls -1t analysis_*.md 2>/dev/null | head -n1 || true)
if [ -n "${AN:-}" ] && [ -f "$AN" ]; then
  sed -i '/^=== \[SNAPSHOT ДЛЯ LLM] ===$/,/^==========================$/d' "$AN" || true
fi

echo "✅ DAY report: $OUT"
