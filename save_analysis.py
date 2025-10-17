#!/usr/bin/env python3
import re, sys, pathlib, datetime
data = sys.stdin.read()
# ищем начало JSON по ключу "time_msk" внутри { ... }
m = re.search(r'\{\s*"time_msk"\s*:', data, re.DOTALL)
analysis = data[:m.start()].rstrip() if m else data.rstrip()
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
path = pathlib.Path(f"analysis_{ts}.md")
path.write_text(analysis + "\n", encoding="utf-8")
print(str(path))
