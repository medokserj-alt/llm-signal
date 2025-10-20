#!/usr/bin/env python3
import sys, re, os
from datetime import datetime, timezone

def main():
    data = sys.stdin.read()

    # 1) Обрезаем всё, что идёт после первого JSON-блока
    cut_idx = data.find("\n{")
    if cut_idx != -1:
        analysis = data[:cut_idx]
    else:
        # иногда модель ставит ```json перед { — обрежем по первой строке начинающейся с {
        m = re.search(r'(?m)^\s*\{', data)
        analysis = data[:m.start()] if m else data

    # 2) Удаляем markdown-кодфенсы (```json / ``` / ```anything)
    analysis = re.sub(r'```+\w*\s*\n?', '', analysis)

    # 3) Убираем возможные хвосты повторного SNAPSHOT/дубликаты JSON-меток
    # (на случай если модель продублировала заголовок перед JSON)
    # ничего агрессивного — только чистка лишних обратных кавычек и меток
    analysis = re.sub(r'(?m)^\s*JSON-сигнал:\s*$', '', analysis)

    # 4) Сжимаем избыточные пустые строки
    analysis = re.sub(r'\n{3,}', '\n\n', analysis).strip() + "\n"

    # 5) Сохраняем с меткой времени (UTC)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"analysis_{ts}.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(analysis)

    print(f"[OK] ANALYSIS: {fname}")

if __name__ == "__main__":
    main()
