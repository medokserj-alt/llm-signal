from typing import List, Dict, Any


def fetch_price_series(symbol: str, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """
    Заглушка получения цен.
    Ожидаемый формат возвращаемых данных:
    [
      {"ts": 1731698400, "price": 0.087},
      {"ts": 1731698460, "price": 0.0885},
      ...
    ]

    TODO: здесь нужно будет подключить реальный источник:
      - Bybit / MEXC / CMC / ваш внутренний сервис
      - или локальное хранилище OHLC

    Пока возвращаем пустой список → evaluator поставит outcome = NO_DATA.
    """
    return []
