#!/bin/bash
set -e

MODEL=${1:-gpt-4.1-mini}

echo "=== [1] Генерация JSON через $MODEL ==="
python3 get_signal_json.py --model "$MODEL" --params params.json | tee last.json

echo -e "\n=== [2] Рендер strict-текста и HTML ==="
python3 render_strict.py < last.json

echo -e "\n=== [3] Логическая валидация уровней ==="
python3 validate_levels.py < last.json
