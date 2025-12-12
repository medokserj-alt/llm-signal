def _percent_distance(price: float | None, level: float | None) -> float | None:
    """
    Возвращает расстояние от price до level в процентах.
    Если данных нет или price == 0 — возвращает None.
    """
    if price is None or level is None:
        return None
    try:
        if price == 0:
            return None
        return abs(price - level) / price * 100.0
    except TypeError:
        return None


def analyze_ema(snapshot: dict) -> dict:
    """
    Базовый EMA-анализ v3.

    Пока что:
    - достаем current_price и EMA20 M15/H1
    - считаем расстояние до EMA20(M15) и EMA20(H1)
    - возвращаем структуру ema_analysis с заглушками

    Позже сюда добавим:
    - ema_fan_state
    - cardio_state
    - ema_score
    - direction_guard
    """
    symbol = snapshot.get("symbol")
    current_price = snapshot.get("current_price")

    ema_m15 = snapshot.get("ema_m15", {}) or {}
    ema_h1 = snapshot.get("ema_h1", {}) or {}

    ema20_m15 = ema_m15.get("ema20")
    ema20_h1 = ema_h1.get("ema20")

    dist_m15 = _percent_distance(current_price, ema20_m15)
    dist_h1 = _percent_distance(current_price, ema20_h1)

    ema_analysis = {
        "ema_fan_state": "mixed",   # заглушка
        "cardio_state": "flat",     # заглушка
        "ema_score": 0,             # заглушка
        "direction_guard": "both",  # заглушка
        "direction_hint": "neutral",
        "preferred_pivot_ema": "ema20_m15",
        "distance": {
            "dist_to_ema20_m15_percent": dist_m15,
            "dist_to_ema20_h1_percent": dist_h1,
        },
        "overheated": {
            "is_overheated": False,
            "reason": None,
        },
        "notes": f"Базовый EMA-анализ для {symbol or 'unknown'}: рассчитаны только дистанции до EMA20(M15/H1).",
    }

    return ema_analysis
