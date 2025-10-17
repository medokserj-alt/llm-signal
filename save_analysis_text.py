#!/usr/bin/env python3
import sys, re, os
from datetime import datetime
data = sys.stdin.read()
# отрежем последний JSON-блок
idx = data.rfind('{')
text = data[:idx].rstrip() if idx != -1 else data.rstrip()
fname = f"analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
with open(fname, "w", encoding="utf-8") as f:
    f.write(text + "\n")
print(fname)
