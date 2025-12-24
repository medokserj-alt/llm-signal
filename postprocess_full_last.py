#!/usr/bin/env python3
import json
from datetime import datetime
from pathlib import Path

from postprocess import process as pp_process
from get_signal_json import (
    get_ema20_m15,
    get_ema20_h1,
    get_ema_provenance,
    apply_ema_relation_flags,
    enforce_ema_narrative_consistency,
    apply_ema_exhale_filter,
    normalize_no_trade,
    validate_or_fallback_tvh_by_mode,
    validate_active_mode_setup,
)
from no_trade_explain import ensure_decision_path

BASE = Path(__file__).resolve().parent

VALID_MODES = ("aggressive", "neutral", "conservative")

def _ema_state(price, em15, em1h) -> str:
    try:
        if price is None or em15 is None or em1h is None:
            return "unknown"
        p = float(price)
        e15 = float(em15)
        e1h = float(em1h)
        if p > e15 and p > e1h:
            return "above_both"
        if p < e15 and p < e1h:
            return "below_both"
        return "between"
    except Exception:
        return "unknown"


def _ema_guard_text(state: str) -> str:
    s = (state or "unknown").strip().lower()
    if s == "above_both":
        return "Цена выше EMA20 на M15 и H1."
    if s == "below_both":
        return "Цена ниже EMA20 на M15 и H1."
    if s == "between":
        return "Цена между EMA20(M15) и EMA20(H1)."
    return "EMA guard: недостаточно данных для определения положения цены относительно EMA."


def _apply_ema_guard_text_consistent(d: dict) -> None:
    state = _ema_state(d.get("price"), d.get("ema20_m15"), d.get("ema20_h1"))
    d["ema_guard"] = _ema_guard_text(state)


def _round_price(val):
    try:
        v = float(val)
    except Exception:
        return None
    av = abs(v)
    prec = 2 if av >= 1 else (4 if av >= 0.01 else 6)
    return round(v, prec)

def _mid_from_range(r):
    if isinstance(r, dict):
        mn = r.get("min")
        mx = r.get("max")
        try:
            a = float(mn)
            b = float(mx)
        except Exception:
            return None
        if b < a:
            a, b = b, a
        return _round_price((a + b) / 2.0)
    if isinstance(r, (list, tuple)) and len(r) == 2:
        try:
            a = float(r[0])
            b = float(r[1])
        except Exception:
            return None
        if b < a:
            a, b = b, a
        return _round_price((a + b) / 2.0)
    return None

def _entry_price_by_mode(d: dict, mode: str):
    k = f"entry_price_{mode}"
    if d.get(k) is not None:
        try:
            return float(d.get(k))
        except Exception:
            pass

    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}
    mid = _mid_from_range(bucket.get("range"))
    if mid is not None:
        d[k] = mid
        return float(mid)

    # fallback: общий entry_range
    mid = _mid_from_range(d.get("entry_range"))
    if mid is not None:
        d[k] = mid
        return float(mid)

    # last resort: текущая цена
    try:
        px = float(d.get("price"))
    except Exception:
        px = None
    if px is not None:
        d[k] = _round_price(px)
        return float(px)
    return None

def _as_float(x):
    try:
        return float(x)
    except Exception:
        return None

def _calc_rr(entry: float, sl: float, tvh1: float) -> float | None:
    risk = abs(entry - sl)
    if not risk:
        return None
    return abs(tvh1 - entry) / risk

def _is_long(d: dict) -> bool | None:
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side == "long":
        return True
    if side == "short":
        return False
    return None

def _gen_tvh(entry: float, sl: float, rr_mult: float, *, is_long: bool) -> float:
    risk = abs(entry - sl)
    delta = rr_mult * risk
    return entry + delta if is_long else entry - delta

def _ensure_by_mode_levels(d: dict) -> dict:
    # Backward-compatible wrapper for older callers.
    return validate_or_fallback_tvh_by_mode(d)

def read_latest_report_text(root_dir: str, limit_chars: int = 2000):
    """
    Читает последний analysis_*.md из reports/day или reports/mid.
    Используется как мягкий контекст (вариант A).
    """
    try:
        base = BASE / "reports" / root_dir
        roots = sorted(base.glob("*"))
        if not roots:
            return "", None
        d = roots[-1]
        an = sorted(d.glob("analysis_*.md"))
        if not an:
            return "", None

        report_time = None
        try:
            report_time = datetime.strptime(d.name, "%Y%m%d_%H%M%S")
        except ValueError:
            pass
        if report_time is None:
            report_time = datetime.fromtimestamp(d.stat().st_mtime)

        now = datetime.now()
        age_hours = max((now - report_time).total_seconds() / 3600, 0.0)

        txt = an[-1].read_text(encoding="utf-8").strip()
        if limit_chars and len(txt) > limit_chars:
            return txt[-limit_chars:], age_hours
        return txt, age_hours
    except Exception:
        return "", None

def main():
    p = BASE / "logs" / "last.json"
    if not p.exists():
        print("postprocess_full_last: logs/last.json not found")
        return

    data = json.loads(p.read_text(encoding="utf-8"))

    # 1) EMA20(M15/H1) для FULL, если отсутствуют
    try:
        sym = data.get("symbol")
        if sym:
            prov_m15 = get_ema_provenance(20, "15m", symbol=sym)
            if prov_m15.get("ema") is not None:
                data["ema20_m15"] = prov_m15.get("ema")
            elif not data.get("ema20_m15"):
                data["ema20_m15"] = get_ema20_m15(sym)

            if prov_m15.get("exchange") is not None:
                data["exchange"] = prov_m15.get("exchange")
            if prov_m15.get("market_type") is not None:
                data["market_type"] = prov_m15.get("market_type")
            if prov_m15.get("price_source") is not None:
                data["price_source"] = prov_m15.get("price_source")
            if prov_m15.get("timeframe") is not None:
                data["timeframe_m15"] = prov_m15.get("timeframe")
            if prov_m15.get("candles_count") is not None:
                data["candles_m15_count"] = prov_m15.get("candles_count")
            if prov_m15.get("last_candle") is not None:
                data["last_candle_m15"] = prov_m15.get("last_candle")
            if prov_m15.get("closes_tail") is not None:
                data["closes_m15_tail"] = prov_m15.get("closes_tail")

            if not data.get("ema20_h1"):
                data["ema20_h1"] = get_ema20_h1(sym)
    except Exception:
        pass

    # 1b) ema_guard текст должен соответствовать рассчитанным EMA/price (без изменения остального текста)
    try:
        _apply_ema_guard_text_consistent(data)
    except Exception:
        pass
    try:
        apply_ema_relation_flags(data)
        enforce_ema_narrative_consistency(data)
    except Exception:
        pass

    # 2) Читаем DAY/MID тексты (вариант A)
    day_txt, day_age_hours = read_latest_report_text("day", limit_chars=2000)
    mid_txt, mid_age_hours = read_latest_report_text("mid", limit_chars=2000)

    day_context = day_txt
    if day_age_hours is not None and day_age_hours > 36:
        print(f"postprocess_full_last: DAY report too old ({day_age_hours:.1f}h), ignoring context")
        day_context = None

    mid_context = mid_txt
    if mid_age_hours is not None and mid_age_hours > 120:
        print(f"postprocess_full_last: MID report too old ({mid_age_hours:.1f}h), ignoring context")
        mid_context = None

    # 3) прогоняем общий v2-процессор
    data = pp_process(data, day_context, mid_context)
    try:
        apply_ema_exhale_filter(data)
    except Exception:
        pass
    try:
        apply_ema_relation_flags(data)
        enforce_ema_narrative_consistency(data)
    except Exception:
        pass
    normalize_no_trade(data)
    _ensure_by_mode_levels(data)
    validate_active_mode_setup(data)
    normalize_no_trade(data)
    ensure_decision_path(data)

    # 4) сохраняем обратно
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("postprocess_full_last: OK")

if __name__ == "__main__":
    main()
