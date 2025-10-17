#!/usr/bin/env python3
import sys, json

data = sys.stdin.read()
if not data.strip():
    print("ERR: empty stdin", file=sys.stderr)
    sys.exit(1)

# Возьмём ПОСЛЕДНИЙ сбалансированный JSON-объект по стеку
last_obj = None
depth = 0
start = None
for i, ch in enumerate(data):
    if ch == '{':
        if depth == 0:
            start = i
        depth += 1
    elif ch == '}':
        if depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                last_obj = data[start:i+1]

if last_obj is None:
    print("ERROR: JSON block not found", file=sys.stderr)
    sys.exit(1)

# Пробуем распарсить (без правок цены/времени)
try:
    obj = json.loads(last_obj.replace('\u2212','-'))
except Exception as e:
    print(f"ERROR: cannot parse JSON: {e}", file=sys.stderr)
    sys.exit(1)

print(json.dumps(obj, ensure_ascii=False))
