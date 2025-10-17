#!/usr/bin/env bash
set -euo pipefail
MODEL="${1:-gpt-4.1-mini}"

# 1) Получаем анализ+JSON
python3 get_signal_json.py --model "$MODEL" --multi | tee _multi_raw.txt

# 2) Сохраняем analysis_*.md (до JSON)
python3 save_analysis.py < _multi_raw.txt | sed 's/^/[OK] ANALYSIS: /'

# 3) Достаём JSON и рендерим
python3 extract_json.py < _multi_raw.txt | tee last.json | python3 render_strict.py
python3 logical_check.py last.json
