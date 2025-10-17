#!/usr/bin/env python3
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import ccxt

def now_msk():
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")

with open("last.json","r",encoding="utf-8") as f:
    obj = json.load(f)

symbol = obj.get("symbol")
bybit, binance = ccxt.bybit(), ccxt.binance()
price = None
for ex in (bybit, binance):
    try:
        t = ex.fetch_ticker(symbol)
        price = t.get("last") or ( (t.get("bid")+t.get("ask"))/2 if t.get("bid") and t.get("ask") else None )
        if price is not None: break
    except Exception:
        pass

if price is not None:
    obj["price"] = float(price)
obj["time_msk"] = now_msk()

with open("last.json","w",encoding="utf-8") as f:
    json.dump(obj, f, ensure_ascii=False, indent=2)
print(f"refreshed: {symbol} price={obj.get('price')} time_msk={obj['time_msk']}")
