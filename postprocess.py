# ===============================================
# postprocess.py — единая логика v2 для SINGLE/FULL
# ===============================================
import json
import re
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
    raw_ctx = d.get("day_mid_context")
    default_ctx = {
        "day_bias": None,
        "mid_bias": None,
        "notes": None,
    }
    if isinstance(raw_ctx, dict):
        ctx = raw_ctx
    elif isinstance(raw_ctx, str):
        s = raw_ctx.strip()
        ctx = dict(default_ctx)
        ctx["notes"] = s or None
    elif raw_ctx is None:
        ctx = dict(default_ctx)
    else:
        ctx = dict(default_ctx)
        try:
            ctx["notes"] = str(raw_ctx)
        except Exception:
            ctx["notes"] = None

    if "day_bias" not in ctx:
        ctx["day_bias"] = None
    if "mid_bias" not in ctx:
        ctx["mid_bias"] = None
    if "notes" not in ctx:
        ctx["notes"] = None

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

def apply_day_mid_intraday_override_note(d: dict) -> None:
    """
    DAY/MID — это bias (prior), а не жёсткий фильтр.
    Если intraday структура сильно противоречит DAY/MID и мы торгуем против bias — добавляем короткую пометку.
    """
    ctx = d.get("day_mid_context")
    if not isinstance(ctx, dict):
        return
    day_bias = str(ctx.get("day_bias") or "").strip().lower()
    mid_bias = str(ctx.get("mid_bias") or "").strip().lower()
    bias = day_bias if day_bias in ("long", "short") else (mid_bias if mid_bias in ("long", "short") else "")
    if bias not in ("long", "short"):
        return

    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short") or side == bias:
        return

    vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
    fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
    strong_contradiction = bool(
        (bias == "long" and vs_h1 == "below" and fan_h1 != "bull")
        or (bias == "short" and vs_h1 == "above" and fan_h1 != "bear")
    )
    if not strong_contradiction:
        return

    ctx["override_note"] = "⚠️ Расхождение с DAY/MID: intraday структура важнее, торгуем по текущей фазе."
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

def _msk_minutes_from_time_str(time_msk_val) -> int | None:
    try:
        s = str(time_msk_val or "").strip()
    except Exception:
        return None
    m = re.findall(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    hh_s, mm_s = m[-1]
    try:
        hh = int(hh_s)
        mm = int(mm_s)
    except Exception:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def _in_danger_time_window_msk(mins: int) -> bool:
    # ВАЖНО: не добавлять новые окна.
    windows = (
        (0, 2 * 60),  # 00:00–02:00
        (8 * 60, 9 * 60),  # 08:00–09:00
        (17 * 60 + 25, 17 * 60 + 45),  # 17:25–17:45
        (18 * 60 + 55, 19 * 60 + 10),  # 18:55–19:10
    )
    return any(start <= mins < end for start, end in windows)


def _tw_news_stress(news_ctx) -> bool:
    if not isinstance(news_ctx, list) or not news_ctx:
        return False
    keywords = (
        "cpi",
        "inflation",
        "fomc",
        "fed",
        "powell",
        "rate",
        "nfp",
        "jobs",
        "sec",
        "lawsuit",
        "etf outflow",
        "liquidation",
    )
    for it in news_ctx:
        s = str(it or "").strip()
        if not s:
            continue
        low = s.lower()
        if re.search(r"impact\\s*:\\s*[-−]", low, flags=re.IGNORECASE):
            return True
        if any(k in low for k in keywords):
            return True
    return False


def _tw_volatility_stress(warnings) -> bool:
    if not isinstance(warnings, list) or not warnings:
        return False
    for w in warnings:
        low = str(w or "").strip().lower()
        if not low:
            continue
        if "impulse_no_exhale" in low:
            return True
        if "ema_source_suspect" in low:
            return True
        if re.search(r"overextended_(no_exhale|h1)\b", low):
            return True
    return False


def _tw_structure_stress(d: dict) -> bool:
    ss = d.get("structure_state")
    return isinstance(ss, str) and ss.strip().lower() == "chaotic"


def _tw_risk_off_stress(d: dict) -> bool:
    if "risk_off" in d:
        return bool(d.get("risk_off"))
    mc = d.get("market_context")
    return isinstance(mc, str) and ("risk-off" in mc.lower())

def _tw_append_unique(d: dict, key: str, value: str) -> None:
    xs = d.get(key)
    if not isinstance(xs, list):
        return
    if value not in xs:
        xs.append(value)


def _tw_has_any_reason(d: dict, *needles: str) -> bool:
    reasons = d.get("no_trade_reasons")
    if not isinstance(reasons, list) or not reasons:
        return False
    for r in reasons:
        if not isinstance(r, str):
            continue
        low = r.strip().lower()
        for n in needles:
            if low == n or n in low:
                return True
    return False


def _tw_neutral_green_lane(d: dict) -> bool:
    if bool(d.get("no_trade")):
        return False

    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return False

    vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
    fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
    fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()

    continuation = False
    if side == "long":
        continuation = (vs_h1 == "above") and (fan_h1 == "bull") and (fan_m15 == "bull")
    elif side == "short":
        continuation = (vs_h1 == "below") and (fan_h1 == "bear") and (fan_m15 == "bear")
    if not continuation:
        return False

    warnings = d.get("warnings")
    if isinstance(warnings, list):
        wl = " ".join(str(w or "").strip().lower() for w in warnings)
        if any(k in wl for k in ("impulse_no_exhale", "phase_between", "ema_between_m15_h1", "ema_source_suspect")):
            return False
        if re.search(r"overextended_(no_exhale|h1)\b", wl):
            return False

    if _tw_structure_stress(d):
        return False
    if _tw_news_stress(d.get("news_context")):
        return False
    if _tw_volatility_stress(warnings):
        return False
    if _tw_risk_off_stress(d):
        return False

    return True


def apply_time_window_policy_variant_b(d: dict) -> None:
    mode = (d.get("mode") or "").strip().lower()
    if mode not in ("aggressive", "neutral", "conservative"):
        mode = "neutral"

    mins = _msk_minutes_from_time_str(d.get("time_msk"))
    if mins is None or not _in_danger_time_window_msk(mins):
        return

    d.setdefault("warnings", [])
    d.setdefault("no_trade_reasons", [])
    d.setdefault("no_trade_hint", "")

    if mode == "aggressive":
        _tw_append_unique(d, "warnings", "time_window_low_liquidity")
        _tw_append_unique(d, "warnings", "time_window_caution_aggressive")
        d["entry_mode"] = "wait_confirm"

        warnings = d.get("warnings")
        stress_news = _tw_news_stress(d.get("news_context"))
        stress_vol = _tw_volatility_stress(warnings)
        stress_struct = _tw_structure_stress(d)
        stress_risk_off = _tw_risk_off_stress(d)
        stress = bool(stress_news or stress_vol or stress_struct or stress_risk_off)

        flush_extreme = _tw_has_any_reason(d, "flush_knife_aggressive_extreme") or bool(d.get("flush_knife_aggressive_extreme"))
        invalid_setup = _tw_has_any_reason(d, "invalid_mode_setup")

        if bool(stress or flush_extreme or invalid_setup):
            d["no_trade"] = True
            _tw_append_unique(d, "no_trade_reasons", "time_window_extreme_block")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "time_window_extreme_block"
        return

    _tw_append_unique(d, "warnings", "time_window_low_liquidity")

    if mode == "conservative":
        d["no_trade"] = True
        reasons = d.get("no_trade_reasons")
        if isinstance(reasons, list) and "time_window" not in reasons:
            reasons.append("time_window")
        d["no_trade_hint"] = "Опасное окно времени (пониженная ликвидность): режим conservative — без сделок."
        return

    # neutral: time_window is an active blocker by default
    if _tw_neutral_green_lane(d):
        _tw_append_unique(d, "warnings", "time_window_green_lane")
        return

    d["no_trade"] = True
    _tw_append_unique(d, "no_trade_reasons", "time_window")
    if not (d.get("no_trade_hint") or "").strip():
        d["no_trade_hint"] = "time_window"

def process(d: dict, day_txt: str | None = None, mid_txt: str | None = None) -> dict:
    """
    Универсальная постобработка v2 для SINGLE/FULL:
    - ema_guard
    - day_mid_context (вариант A)
    - adx_guard (stub)
    - entries (если ещё нет)
    - no_trade defaults
    - time-window policy (вариант B)
    """
    apply_ema_guard(d)
    apply_day_mid_context(d, day_txt, mid_txt)
    apply_day_mid_intraday_override_note(d)
    apply_adx_guard(d)
    ensure_no_trade_defaults(d)
    build_entries_if_missing(d)
    apply_time_window_policy_variant_b(d)
    return d
