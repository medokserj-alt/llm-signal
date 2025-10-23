#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
FILE="auto_feedback/lessons/Auto_Lessons.md"
# Добавляем апдейт от Ани одной строкой аргумента: ./tools/anya_lessons_append.sh "- правило ..."
echo -e "\n# Update $(date -u +%Y-%m-%dT%H:%M:%SZ)\n$*" >> "$FILE"
python3 - <<'PY'
from feedback_writer import rebuild_lessons_md
n = rebuild_lessons_md()
print(f"[DONE] Auto_Lessons appended; LESSONS rebuilt with {n} feedback entries + Auto_Lessons.md")
PY
