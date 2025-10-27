#!/usr/bin/env python3
import re, sys, pathlib, datetime
from lessons_block import load_lessons

LESSONS_RE = re.compile(r'LESSONS[\s\S]*?(?=\n\s*\n|\Z)', re.MULTILINE)

def replace_lessons_block(text: str) -> str:
    try:
        lessons_text, _, _ = load_lessons(0)  # без лимита + AUTO_LESSONS
        return LESSONS_RE.sub(lessons_text.strip(), text)
    except Exception:
        return text

data = sys.stdin.read()

# ищем начало JSON по ключу "time_msk" внутри { ... }
m = re.search(r'\{\s*"time_msk"\s*:', data, re.DOTALL)
analysis = data[:m.start()].rstrip() if m else data.rstrip()

# Подмена [LESSONS] на полный текст
analysis = replace_lessons_block(analysis)

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
path = pathlib.Path(f"analysis_{ts}.md")
path.write_text(analysis + "\n", encoding="utf-8")
print(str(path))
