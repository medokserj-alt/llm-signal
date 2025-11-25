import time
from typing import List, Dict, Any, Optional

import requests


BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"


def _fetch_bybit_klines(
    symbol: str,
    start_ts: int,
    end_ts: int,
    interval: str = "1",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Запрос kline-данных с Bybit (public API).

    interval: "1" = 1 минута, "3" = 3m, "5" = 5m и т.д.
    Документация: /v5/market/kline

    Для простоты:
    - не делаем сложную пагинацию,
    - полагаемся на то, что окна у нас небольшие (несколько часов).
    """
    start_ms = start_ts * 1000
    end_ms = end_ts * 1000

    params = {
        "category": "linear",   # фьючерсы/перпеты; для spot можно "spot"
        "symbol": symbol,
        "interval": interval,
        "start": start_ms,
        "end": end_ms,
        "limit": limit,
    }

    try:
        resp = requests.get(BYBIT_KLINE_URL, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        # Логика: при любой ошибке просто возвращаем пустой список,
        # evaluator сам выдаст outcome = NO_DATA.
        print(f"[market_data] error fetching klines from Bybit: {e}")
        return []

    if data.get("retCode") != 0:
        print(f"[market_data] Bybit error: {data.get('retMsg')}")
        return []

    rows = data.get("result", {}).get("list", []) or []
    # Bybit отдаёт строки в порядке от новых к старым, развернём:
    rows = list(reversed(rows))

    klines: List[Dict[str, Any]] = []
    for row in rows:
        try:
            # Формат: [startTime, open, high, low, close, volume, turnover]
            start_time_ms = int(row[0])
            close_price = float(row[4])
            ts_sec = start_time_ms // 1000
            if start_ts <= ts_sec <= end_ts:
                klines.append({"ts": ts_sec, "price": close_price})
        except Exception:
            continue

    return klines


def fetch_price_series(symbol: str, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """
    Основная точка для evaluator'а.
    Возвращает список {ts, price} для указанного символа и окна.

    Сейчас используем Bybit linear kline, interval=1m.
    При необходимости можно:
    - переключить category на spot,
    - изменить interval,
    - добавить кэширование.
    """
    if start_ts >= end_ts:
        return []

    # На будущее: маппинг символов, если нужно (например, SUIUSDT → SUIUSDT).
    bybit_symbol = symbol

    series = _fetch_bybit_klines(
        symbol=bybit_symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        interval="1",
        limit=200,
    )

    # Уже отсортированы по времени, но на всякий случай:
    series.sort(key=lambda x: x["ts"])
    return series
