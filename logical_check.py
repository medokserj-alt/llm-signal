#!/usr/bin/env python3
import sys, json, math, pathlib

if len(sys.argv) < 2:
    print("Usage: logical_check.py <signal.json>", file=sys.stderr); sys.exit(2)

p = pathlib.Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))

err = []

def num(x): 
    try: return float(x)
    except: return None

# обязательные поля
need = ["symbol","price","direction","entry_range","sl","tp1","tp2","rr","validity_minutes"]
for k in need:
    if k not in data: err.append(f"missing field: {k}")

if "entry_range" in data:
    mn = num(data["entry_range"].get("min"))
    mx = num(data["entry_range"].get("max"))
    if mn is None or mx is None: err.append("entry_range must have numeric min/max")
    elif not (mn < mx): err.append(f"entry_range invalid: min={mn}, max={mx}")

price = num(data.get("price"))
sl    = num(data.get("sl"))
tp1   = num(data.get("tp1"))
tp2   = num(data.get("tp2"))
rr    = num(data.get("rr"))
direction = (data.get("direction") or "").lower()

if direction in ("long","short"):
    if None not in (price, sl):
        if direction=="long" and not (sl < price): err.append(f"long: SL {sl} must be < price {price}")
        if direction=="short" and not (sl > price): err.append(f"short: SL {sl} must be > price {price}")
    if None not in (tp1, tp2):
        if direction=="long" and not (tp1 > price and tp2 > tp1): err.append("long: TP1>price and TP2>TP1 required")
        if direction=="short" and not (tp1 < price and tp2 < tp1): err.append("short: TP1<price and TP2<TP1 required")
else:
    err.append(f"unknown direction: {direction}")

if rr is None or rr < 1.5:
    err.append(f"rr too low: {rr}")

vm = data.get("validity_minutes")
if not isinstance(vm, int) or vm <= 0 or vm > 360:
    err.append(f"validity_minutes invalid: {vm}")

if err:
    print("FAIL: logical checks failed:")
    for e in err: print(" -", e)
    sys.exit(1)
else:
    print("OK: logical checks passed.")
    sys.exit(0)
