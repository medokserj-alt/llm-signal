import json
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

from app.agent.market_data import fetch_price_series

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "journal.jsonl"


def load_journal() -> List[Dict[str, Any]]:
    """Читает весь журнал в память."""
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def write_journal_event(kind: str, event: Dict[str, Any]) -> None:
    """Запись нового события."""
    record = {
        "ts": time.time(),
        "kind": kind,
        "event": event,
    }
    LOG_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_ts(ts_str: str) -> Optional[int]:
    """Парсим строку вида 2025-11-16T08:30:00Z в unix time."""
    try:
        return int(time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def _determine_outcome(signal: Dict[str, Any],
                       prices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    На основе цен определяем исход:
    TP1 / TP2 / SL / NO_TRIGGER / TIMEOUT / NO_DATA
    """
    symbol = signal.get("symbol")
    direction = signal.get("direction")
    entry_zone = signal.get("entry_zone", [])
    sl = signal.get("sl")
    tp_conf = signal.get("tp", {})
    tp1 = tp_conf.get("tp1")
    tp2 = tp_conf.get("tp2")

    if not prices:
        return {
            "outcome": "NO_DATA",
            "reason": "no_price_data",
        }

    if not (isinstance(entry_zone, list) and len(entry_zone) == 2):
        return {
            "outcome": "INVALID_SIGNAL",
            "reason": "invalid_entry_zone",
        }

    entry_low, entry_high = float(entry_zone[0]), float(entry_zone[1])
    sl = float(sl) if sl is not None else None
    tp1 = float(tp1) if tp1 is not None else None
    tp2 = float(tp2) if tp2 is not None else None

    # Ожидаем формат цены: {"ts": ..., "price": ...}
    series: List[Tuple[int, float]] = []
    for p in prices:
        try:
            ts = int(p["ts"])
            price = float(p["price"])
            series.append((ts, price))
        except Exception:
            continue

    if not series:
        return {
            "outcome": "NO_DATA",
            "reason": "empty_price_series",
        }

    # 1) ищем момент входа: первая цена в зоне entry_zone
    entry_idx = None
    entry_ts = None
    entry_price = None

    for idx, (ts, price) in enumerate(series):
        if entry_low <= price <= entry_high:
            entry_idx = idx
            entry_ts = ts
            entry_price = price
            break

    if entry_idx is None:
        # Цена так и не зашла в зону
        return {
            "outcome": "NO_TRIGGER",
            "reason": "price_never_touched_entry_zone",
        }

    # 2) после входа смотрим, что произошло первым: TP1/TP2/SL
    post_series = series[entry_idx + 1 :]
    hits: List[Tuple[int, str, int, float]] = []  # (idx, level, ts, price)

    for idx, (ts, price) in enumerate(post_series):
        # long / short разные условия
        if direction == "long":
            if tp1 is not None and price >= tp1:
                hits.append((idx, "TP1", ts, price))
            if tp2 is not None and price >= tp2:
                hits.append((idx, "TP2", ts, price))
            if sl is not None and price <= sl:
                hits.append((idx, "SL", ts, price))
        elif direction == "short":
            if tp1 is not None and price <= tp1:
                hits.append((idx, "TP1", ts, price))
            if tp2 is not None and price <= tp2:
                hits.append((idx, "TP2", ts, price))
            if sl is not None and price >= sl:
                hits.append((idx, "SL", ts, price))

    if not hits:
        # Никто не сработал в окне
        return {
            "outcome": "TIMEOUT",
            "entry_price": entry_price,
            "entry_ts": entry_ts,
            "reason": "no_level_hit_in_window",
        }

    # Берём самое раннее срабатывание
    hits.sort(key=lambda h: h[0])
    _, level, exit_ts, exit_price = hits[0]

    # PnL в %
    if entry_price is None:
        pnl_pct = None
    else:
        if direction == "long":
            pnl_pct = (exit_price / entry_price - 1) * 100
        elif direction == "short":
            pnl_pct = (entry_price / exit_price - 1) * 100
        else:
            pnl_pct = None

    return {
        "outcome": level,
        "hit_level": level.lower(),
        "entry_price": entry_price,
        "entry_ts": entry_ts,
        "exit_price": exit_price,
        "exit_ts": exit_ts,
        "pnl_pct": pnl_pct,
        "reason": "level_hit",
        "symbol": symbol,
        "direction": direction,
    }


def evaluate_signals(window_minutes: int = 1) -> Dict[str, Any]:
    """
    Оценка сигналов:
    - берём signals старше window_minutes
    - для каждой пытаемся определить outcome по ценам
    - пишем trade_result
    """
    now = time.time()
    journal = load_journal()

    signals = []
    for rec in journal:
        if rec["kind"] != "signal":
            continue

        event = rec["event"]
        published_at_str = event.get("published_at")
        published_ts = _parse_ts(published_at_str) if published_at_str else None
        if not published_ts:
            continue

        if now - published_ts <= window_minutes * 60:
            # ещё рано оценивать
            continue

        signals.append(event)

    created_results = []

    for sig in signals:
        signal_id = sig.get("signal_id")
        if not signal_id:
            continue

        # уже есть результат?
        already = any(
            rec["kind"] == "trade_result"
            and rec["event"].get("signal_id") == signal_id
            for rec in journal
        )
        if already:
            continue

        symbol = sig.get("symbol")
        published_at_str = sig.get("published_at")
        published_ts = _parse_ts(published_at_str) if published_at_str else None
        if not symbol or not published_ts:
            continue

        # окно: с момента сигнала до текущего времени
        start_ts = published_ts
        end_ts = int(now)

        prices = fetch_price_series(symbol, start_ts, end_ts)
        outcome_info = _determine_outcome(sig, prices)

        result_event: Dict[str, Any] = {
            "signal_id": signal_id,
            "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "symbol": symbol,
            "window_minutes": window_minutes,
        }
        result_event.update(outcome_info)

        write_journal_event("trade_result", result_event)
        created_results.append(result_event)

    return {"evaluated": len(created_results), "results": created_results}
