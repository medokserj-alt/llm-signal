#!/usr/bin/env python3
import os; os.chdir("/root/llm-signal-clone")
from pathlib import Path

def get_last_reports():
    base = Path(__file__).resolve().parent / "reports"
    out = {}
    for kind in ("day", "mid"):
        dirs = sorted((base / kind).glob("*"))
        if not dirs:
            continue
        last_dir = dirs[-1]
        files = sorted(last_dir.glob("analysis_*.md"))
        if files:
            out[kind] = files[-1].read_text(encoding="utf-8").strip()
    return out

if __name__ == "__main__":
    data = get_last_reports()
    for k, v in data.items():
        print(f"=== LAST {k.upper()} ===")
        print(v[:800] + ("..." if len(v) > 800 else ""))
        print()
