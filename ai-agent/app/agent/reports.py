import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "journal.jsonl"


def _load_journal() -> List[Dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _find_signal_and_result(signal_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    journal = _load_journal()
    signal = None
    result = None

    for rec in journal:
        if rec.get("kind") == "signal" and rec.get("event", {}).get("signal_id") == signal_id:
            signal = rec.get("event")
        if rec.get("kind") == "trade_result" and rec.get("event", {}).get("signal_id") == signal_id:
            result = rec.get("event")

    return signal, result


def build_trade_report(signal_id: str) -> Dict[str, Any]:
    """
    Собирает структурированный отчёт по сделке + текстовый summary.
    """
    signal, result = _find_signal_and_result(signal_id)
    if signal is None:
        return {
            "ok": False,
            "reason": "signal_not_found",
            "signal_id": signal_id,
        }

    base: Dict[str, Any] = {
        "ok": True,
        "signal_id": signal_id,
        "signal": signal,
        "result": result,
    }

    if result is None:
        base["status"] = "no_result"
        base["summary"] = f"Для сигнала {signal_id} ещё нет оценки (trade_result)."
        return base

    outcome = result.get("outcome")
    symbol = result.get("symbol") or signal.get("symbol")
    direction = result.get("direction") or signal.get("direction")
    pnl_pct = result.get("pnl_pct")
    reason = result.get("reason")
    hit_level = result.get("hit_level")

    if outcome == "NO_DATA":
        summary = (
            f"Сигнал {signal_id} по {symbol} ({direction}).\n"
            f"Пока не удалось загрузить ценовые данные для оценки результата (NO_DATA: {reason})."
        )
    elif outcome == "NO_TRIGGER":
        summary = (
            f"Сигнал {signal_id} по {symbol} ({direction}).\n"
            f"Цена ни разу не зашла в зону входа — сетап не активировался (NO_TRIGGER)."
        )
    elif outcome == "TIMEOUT":
        summary = (
            f"Сигнал {signal_id} по {symbol} ({direction}).\n"
            f"Цена зашла в зону входа, но ни TP, ни SL не были достигнуты в заданном окне — TIMEOUT."
        )
    elif outcome in ("TP1", "TP2", "SL"):
        dir_text = "лонг" if direction == "long" else "шорт" if direction == "short" else direction
        pnl_text = f"{pnl_pct:.2f}%" if isinstance(pnl_pct, (int, float)) else "не рассчитан"
        level_text = hit_level or outcome.lower()
        summary = (
            f"Сделка по сигналу {signal_id} по {symbol} ({dir_text}) завершилась на уровне {level_text}.\n"
            f"Результат по оценке агента: {outcome}, приблизительный PnL: {pnl_text}."
        )
    else:
        summary = (
            f"Сигнал {signal_id} по {symbol} ({direction}).\n"
            f"Результат: {outcome or 'UNKNOWN'}, причина: {reason or 'не указана'}."
        )

    base["status"] = "ok"
    base["summary"] = summary
    return base


def build_daily_summary(window_hours: int = 24) -> Dict[str, Any]:
    """
    Сводка по сделкам за последние window_hours часов.
    Сейчас работает даже с NO_DATA — считает только по исходам.
    """
    now = time.time()
    since = now - window_hours * 3600

    journal = _load_journal()
    results: List[Dict[str, Any]] = []

    for rec in journal:
        if rec.get("kind") != "trade_result":
            continue
        ts = rec.get("ts")
        if ts is None or ts < since:
            continue
        results.append(rec.get("event", {}))

    total = len(results)
    by_outcome: Dict[str, int] = {}
    symbols: Dict[str, int] = {}

    for r in results:
        outcome = r.get("outcome") or "UNKNOWN"
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        symbol = r.get("symbol") or "UNKNOWN"
        symbols[symbol] = symbols.get(symbol, 0) + 1

    summary_lines = []
    summary_lines.append(f"Сводка по сделкам за последние {window_hours}ч:")
    summary_lines.append(f"Всего оценённых сигналов: {total}.")

    if total == 0:
        summary_lines.append("За этот период нет ни одной оценённой сделки.")
    else:
        out_parts = [f"{k}: {v}" for k, v in by_outcome.items()]
        summary_lines.append("Исходы по сделкам: " + ", ".join(out_parts))
        sym_parts = [f"{k}: {v}" for k, v in symbols.items()]
        summary_lines.append("Активы (по количеству сделок): " + ", ".join(sym_parts))

    summary_text = "\n".join(summary_lines)

    return {
        "ok": True,
        "window_hours": window_hours,
        "total_trades": total,
        "by_outcome": by_outcome,
        "by_symbol": symbols,
        "summary": summary_text,
    }
