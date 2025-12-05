# ===============================================
# postprocess.py — единая логика v2 для SINGLE/FULL
# ===============================================
import json
from datetime import datetime

def apply_ema_guard(d: dict) -> None:
    """Формирует ema_guard: положение цены относительно EMA20(M15/H1)."""
    em15 = d.get("ema20_m15")
    em1h = d.get("ema20_h1")
    price = d.get("price")
    state = "unknown"

    try:
        if em15 is not None and em1h is not None and price is not None:
            p = float(price)
            e15 = float(em15)
            e1h = float(em1h)
            if p > e15 and p > e1h:
                state = "above_both"
            elif p < e15 and p < e1h:
                state = "below_both"
            else:
                state = "between"
    except Exception:
        state = "unknown"

    d.setdefault("ema_guard", {
        "ema20_m15": em15,
        "ema20_h1": em1h,
        "state": state,
    })

def apply_day_mid_context(d: dict, day_txt: str | None, mid_txt: str | None) -> None:
    """
    Мягкий вариант A: DAY/MID только как фон (bias + короткая пометка), не фильтр.
    Работает и для SINGLE, и для FULL.
    """
    ctx = d.get("day_mid_context") or {
        "day_bias": None,
        "mid_bias": None,
        "notes": None,
    }

    def detect_bias(txt: str | None) -> str | None:
        if not txt:
            return None
        low = txt.lower()
        if "short" in low:
            return "short"
        if "long" in low:
            return "long"
        return "neutral"

    if ctx.get("day_bias") is None:
        ctx["day_bias"] = detect_bias(day_txt)
    if ctx.get("mid_bias") is None:
        ctx["mid_bias"] = detect_bias(mid_txt)

    if not ctx.get("notes") and (day_txt or mid_txt):
        parts = []
        if ctx["day_bias"]:
            parts.append(f"DAY bias: {ctx['day_bias']}")
        if ctx["mid_bias"]:
            parts.append(f"MID bias: {ctx['mid_bias']}")
        ctx["notes"] = "; ".join(parts) if parts else None

    d["day_mid_context"] = ctx

def apply_adx_guard(d: dict) -> None:
    """
    ADX guard — пока только stub: JSON-форма под будущий фильтр силы тренда.
    """
    d.setdefault("adx_guard", {
        "m15": None,
        "strength": "unknown",
    })

def build_entries_if_missing(d: dict) -> None:
    """
    Строим 3 профиля входа (AGG/NEUT/CONS), только если entries ещё нет.
    Это мягкая версия v2 для FULL; SINGLE обычно уже имеет entries.
    """
    if d.get("entries"):
        # SINGLE уже построил через finalize_signal — не трогаем.
        return

    try:
        price = float(d.get("price") or 0)
        er = d.get("entry_range") or {}
        emin, emax = er.get("min"), er.get("max")
        if emin is None or emax is None or not price:
            return

        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if side not in ("long", "short"):
            return

        cmin = float(emin)
        cmax = float(emax)
        if cmax < cmin:
            cmin, cmax = cmax, cmin

        mid = (cmin + cmax) / 2
        width = cmax - cmin
        if width <= 0:
            width = max(abs(mid) * 0.001, 0.0005)

        # Базовый нейтральный диапазон = текущий entry_range
        neutral_range = {"min": round(cmin, 6), "max": round(cmax, 6)}

        # Консервативный: глубже по тренду
        if side == "long":
            cons_min = round(cmin - width, 6)
            cons_max = round(cmax - width, 6)
        else:  # short
            cons_min = round(cmin + width, 6)
            cons_max = round(cmax + width, 6)

        # Агрессивный: ближе к цене
        if side == "long":
            a_min = round(mid + 0.3 * (price - mid), 6)
            a_max = round(min(price, a_min + width * 0.5), 6)
        else:  # short
            a_max = round(mid - 0.3 * (mid - price), 6)
            a_min = round(max(price, a_max - width * 0.5), 6)

        entries = {
            "aggressive": {
                "enabled": True,
                "range": {"min": a_min, "max": a_max},
                "entry_mode": d.get("entry_mode", "limit"),
                "position_size_hint": "0.5x",
                "comment": "Более близкий к текущей цене вход, только по тренду, с повышенным риском.",
            },
            "neutral": {
                "enabled": True,
                "range": neutral_range,
                "entry_mode": d.get("entry_mode", "limit"),
                "position_size_hint": "1.0x",
                "comment": "Основной рабочий вход.",
            },
            "conservative": {
                "enabled": True,
                "range": {"min": cons_min, "max": cons_max},
                "entry_mode": "limit",
                "position_size_hint": "0.75x",
                "comment": "Глубокий откат, более безопасный вход.",
            },
        }

        d["entries"] = entries
    except Exception:
        return

def ensure_no_trade_defaults(d: dict) -> None:
    d.setdefault("no_trade", False)
    d.setdefault("no_trade_reasons", [])
    d.setdefault("no_trade_hint", "")
    d.setdefault("max_valid_minutes", 90)

def apply_soft_time_window_mode(d: dict) -> None:
    """
    Мягкое обращение с time_window:
    - не блокируем сигнал полностью;
    - отключаем агрессивный вход;
    - усиливаем консервативный профиль;
    - добавляем warning.
    """
    reasons = d.get("no_trade_reasons") or []
    if d.get("no_trade") and "time_window" in reasons:
        # Сбрасываем жёсткий no_trade в пользу conservative mode
        d["no_trade"] = False

        warnings = d.get("warnings") or []
        if "time_window_low_liquidity" not in warnings:
            warnings.append("time_window_low_liquidity")
        d["warnings"] = warnings

        entries = d.get("entries") or {}
        # режем aggressive
        agg = entries.get("aggressive")
        if isinstance(agg, dict):
            agg["enabled"] = False
            # чтобы было видно, почему
            comment = (agg.get("comment") or "").strip()
            if "time_window" not in comment:
                agg["comment"] = (comment + " " if comment else "") + "Отключён в time_window: рынок волатилен/тонкий."

        # усиливаем conservative
        cons = entries.get("conservative")
        if isinstance(cons, dict):
            cons["position_size_hint"] = "0.5x"
            comment = (cons.get("comment") or "").strip()
            if "time_window" not in comment:
                cons["comment"] = (comment + " " if comment else "") + "Рекомендуется в time_window как более безопасный вход."

        d["entries"] = entries

def process(d: dict, day_txt: str | None = None, mid_txt: str | None = None) -> dict:
    """
    Универсальная постобработка v2 для SINGLE/FULL:
    - ema_guard
    - day_mid_context (вариант A)
    - adx_guard (stub)
    - entries (если ещё нет)
    - no_trade defaults
    - soft time-window (не рубим сигнал, а переводим в conservative режим)
    """
    apply_ema_guard(d)
    apply_day_mid_context(d, day_txt, mid_txt)
    apply_adx_guard(d)
    ensure_no_trade_defaults(d)
    build_entries_if_missing(d)
    apply_soft_time_window_mode(d)
    return d
