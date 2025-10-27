#!/usr/bin/env python3
import json, pathlib, sys, datetime

BASE = pathlib.Path(__file__).resolve().parent
LESSONS_DIR = BASE / "auto_feedback" / "lessons"
ROLLING = LESSONS_DIR / "rolling.jsonl"
OUT = LESSONS_DIR / "LESSONS_FOR_LLM.md"

K = 5  # сколько последних сниппетов включать

def main():
    LESSONS_DIR.mkdir(parents=True, exist_ok=True)
    if not ROLLING.exists():
        OUT.write_text("", encoding="utf-8")
        print("[LESSONS] rolling.jsonl not found -> empty block")
        return

    lines = [ln.strip() for ln in ROLLING.read_text(encoding="utf-8").splitlines() if ln.strip()]
    tail = lines[-K:] if len(lines) > K else lines

    # Формируем компактный блок для подмешивания в промпт
    items = []
    for ln in tail:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        pair = d.get("pair","?")
        res  = d.get("result","?")
        issues = ", ".join(d.get("issues",[])) or "none"
        fixes  = ", ".join(d.get("fixes",[])) or "none"
        comment = d.get("comment","").strip()
        when = d.get("datetime","")

        items.append(f"- {when} • {pair} • result={res}; issues=[{issues}]; fixes=[{fixes}]"
                     + (f"; note: {comment}" if comment else ""))

    if not items:
        OUT.write_text("", encoding="utf-8")
        print("[LESSONS] no valid items -> empty block")
        return

    header = "# [LESSONS]\n" \
             "Учитывай повторяющиеся ошибки и принятые фиксы при генерации сигнала. " \
             "Ниже последние случаи (свежие внизу):\n\n"
    body = "\n".join(items)
    OUT.write_text(header + body + "\n", encoding="utf-8")
    print(f"[LESSONS] wrote {len(items)} items -> {OUT}")

if __name__ == "__main__":
    main()
