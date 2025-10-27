#!/usr/bin/env python3
import sys, re, os
from datetime import datetime, timezone

def main():
    data = sys.stdin.read()

    cut_idx = data.find("\n{")
    if cut_idx != -1:
        analysis = data[:cut_idx]
    else:
        m = re.search(r'(?m)^\s*\{', data)
        analysis = data[:m.start()] if m else data

    analysis = re.sub(r'```+\w*\s*\n?', '', analysis)
    analysis = re.sub(r'(?m)^\s*JSON-сигнал:\s*$', '', analysis)
    analysis = re.sub(r'\n{3,}', '\n\n', analysis).strip() + "\n"

    lines = analysis.splitlines()
    lines = [ln for ln in lines if not re.match(r'^\s*🔁\s*Альтернативный вход:', ln)]
    analysis = "\n".join(lines).rstrip() + "\n"

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"analysis_{ts}.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(analysis)

    print(f"[OK] ANALYSIS: {fname}")

if __name__ == "__main__":
    main()
