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
        # Игнорируем окна полностью.
        reasons_before = d.get("no_trade_reasons") if isinstance(d.get("no_trade_reasons"), list) else []
        hint_before = d.get("no_trade_hint") if isinstance(d.get("no_trade_hint"), str) else ""
        had_tw = any(isinstance(r, str) and "time_window" in r for r in reasons_before) or ("time_window" in hint_before.lower())

        if isinstance(d.get("warnings"), list):
            d["warnings"] = [
                w for w in d["warnings"] if not (isinstance(w, str) and "time_window" in w)
            ]
        if isinstance(d.get("no_trade_reasons"), list):
            d["no_trade_reasons"] = [
                r for r in d["no_trade_reasons"] if not (isinstance(r, str) and "time_window" in r)
            ]
        if isinstance(d.get("no_trade_hint"), str) and "time_window" in d["no_trade_hint"].lower():
            d["no_trade_hint"] = ""
        if had_tw and bool(d.get("no_trade")) and not d.get("no_trade_reasons") and not d.get("no_trade_hint"):
            d["no_trade"] = False
        return

    warnings = d.get("warnings")
    if isinstance(warnings, list) and "time_window_low_liquidity" not in warnings:
        warnings.append("time_window_low_liquidity")

    if mode == "conservative":
        d["no_trade"] = True
        reasons = d.get("no_trade_reasons")
        if isinstance(reasons, list) and "time_window" not in reasons:
            reasons.append("time_window")
        d["no_trade_hint"] = "Опасное окно времени (пониженная ликвидность): режим conservative — без сделок."
        return

    # neutral
    stress_news = _tw_news_stress(d.get("news_context"))
    stress_vol = _tw_volatility_stress(d.get("warnings"))
    stress_struct = _tw_structure_stress(d)
    stress_risk_off = _tw_risk_off_stress(d)
    stress = bool(stress_news or stress_vol or stress_struct or stress_risk_off)

    if stress:
        d["no_trade"] = True
        reasons = d.get("no_trade_reasons")
        if isinstance(reasons, list) and "time_window" not in reasons:
            reasons.append("time_window")
        tags = []
        if stress_news:
            tags.append("news")
        if stress_vol:
            tags.append("volatility")
        if stress_struct:
            tags.append("chaotic")
        if stress_risk_off:
            tags.append("risk-off")
        tag_str = "/".join(tags) if tags else "stress"
        d["no_trade_hint"] = f"Опасное окно времени + стресс-условия ({tag_str}): режим neutral — пропустить сделку."
        return

    # в окне, но без stress → только warning
    if bool(d.get("no_trade")) and isinstance(d.get("no_trade_reasons"), list):
        had_tw = any(isinstance(r, str) and "time_window" in r for r in d["no_trade_reasons"])
        d["no_trade_reasons"] = [
            r for r in d["no_trade_reasons"] if not (isinstance(r, str) and "time_window" in r)
        ]
        if had_tw and not d["no_trade_reasons"]:
            d["no_trade"] = False
            d["no_trade_hint"] = ""

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
    apply_adx_guard(d)
    ensure_no_trade_defaults(d)
    build_entries_if_missing(d)
    apply_time_window_policy_variant_b(d)
    return d
