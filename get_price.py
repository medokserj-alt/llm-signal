import sys, json
import ccxt

def norm(sym: str) -> str:
    s = sym.strip().upper()
    if '/' in s: return s
    return f"{s}/USDT"

def fetch_price(symbol: str):
    bybit = ccxt.bybit()
    try:
        t = bybit.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        # запасной вариант — binance spot
        binance = ccxt.binance()
        t = binance.fetch_ticker(symbol)
        return float(t["last"])

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 get_price.py <symbol|eth|ETH/USDT>", file=sys.stderr)
        sys.exit(1)
    symbol = norm(sys.argv[1])
    price = fetch_price(symbol)
    print(json.dumps({"symbol": symbol, "price": price}, ensure_ascii=False))
