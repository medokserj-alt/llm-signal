#!/usr/bin/env python3
import os
import sys
import json
import math
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from openai import OpenAI
import ccxt
import subprocess
from pathlib import Path
import pathlib as _pl

VALID_MODES = {"aggressive", "neutral", "conservative"}

# ---- EMA20 helpers ----
def _ema(vals, period=20):
    if not vals or len(vals) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = float(vals[-period])
    for v in vals[-period + 1:]:
        ema = float(v) * k + ema * (1.0 - k)
    return round(ema, 6)

_CLOSES_CACHE: dict[tuple[str, str], dict] = {}


def _normalize_timeframe(timeframe: str) -> str:
    tf = (timeframe or "").strip().lower()
    if tf in {"m15", "15m", "15"}:
        return "15m"
    if tf in {"h1", "1h", "60m", "60"}:
        return "1h"
    return tf


def _fetch_closes(timeframe: str, *, symbol: str, limit: int) -> list[float] | None:
    tf = _normalize_timeframe(timeframe)
    try:
        lim = int(limit)
    except Exception:
        lim = 0
    if lim <= 0:
        return None

    cache_key = (symbol, tf)
    cached = _CLOSES_CACHE.get(cache_key) or {}
    cached_closes = cached.get("closes")
    if isinstance(cached_closes, list) and len(cached_closes) >= lim:
        return cached_closes

    for ex in (ccxt.bybit(), ccxt.binance()):
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe=tf, limit=lim)
            closes = [float(c[4]) for c in ohlcv if len(c) >= 5 and c[4] is not None]
            if closes:
                _CLOSES_CACHE[cache_key] = {"closes": closes}
                return closes
        except Exception:
            pass
    return None


def get_ema(period: int, timeframe: str, *, symbol: str):
    try:
        p = int(period)
    except Exception:
        return None
    if p <= 1:
        return None

    tf = _normalize_timeframe(timeframe)
    # чуть больше минимального окна, чтобы снизить шанс нехватки данных
    limit = max(p + 25, int(p * 1.25))
    closes = _fetch_closes(tf, symbol=symbol, limit=limit)
    if not closes:
        return None
    e = _ema(closes, p)
    return float(e) if e is not None else None


def get_ema20_m15(symbol: str):
    return get_ema(20, "15m", symbol=symbol)


def get_ema20_h1(symbol: str):
    return get_ema(20, "1h", symbol=symbol)


# ---------- utils ----------
def ensure_defaults(d: dict) -> dict:
    """Нормализация полей side и entry_mode перед выводом JSON."""
    em = (d.get("entry_mode") or "").strip().lower()
    if not d.get("side"):
        d["side"] = "long" if em in ("limit", "now", "market") else "flat"
    if em == "now":
        d.setdefault("warnings", []).append("market_entry_high_conf")
        d["entry_mode"] = "market"
    return d


def normalize_mode(mode_val) -> str:
    try:
        m = (mode_val or "").strip().lower()
    except Exception:
        m = ""
    return m if m in VALID_MODES else "neutral"

def normalize_no_trade(d: dict) -> dict:
    """
    Нормализует no_trade-ветку и чистит legacy-маркеры (time_window и тексты про ликвидность)
    без изменения торговой логики (направление/EMA/фильтры).
    """
    def _has_legacy(s: str) -> bool:
        low = (s or "").lower()
        if "time_window" in low:
            return True
        # Удаляем любые явные формулировки про "пониженную ликвидность"
        if "понижен" in low and "ликвид" in low:
            return True
        if "окно" in low and "ликвид" in low:
            return True
        return False

    def _clean_str(s: str) -> str:
        if not isinstance(s, str):
            return ""
        return "" if _has_legacy(s) else s.strip()

    def _clean_list_str(xs) -> list[str]:
        if not isinstance(xs, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for it in xs:
            if not isinstance(it, str):
                continue
            s = it.strip()
            if not s or _has_legacy(s):
                continue
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _walk(x):
        if isinstance(x, dict):
            for k in list(x.keys()):
                v = x[k]
                if k in ("no_trade_hint", "comment", "comments") and isinstance(v, str):
                    x[k] = _clean_str(v)
                    continue
                if k == "no_trade_reasons":
                    x[k] = _clean_list_str(v)
                    continue
                if k == "warnings":
                    x[k] = _clean_list_str(v)
                    continue
                _walk(v)
        elif isinstance(x, list):
            for it in x:
                _walk(it)

    try:
        _walk(d)
    except Exception:
        pass

    # Консистентность: если no_trade == false → reasons=[], hint=""
    if not bool(d.get("no_trade")):
        d["no_trade_reasons"] = []
        d["no_trade_hint"] = ""
    else:
        d["no_trade_reasons"] = _clean_list_str(d.get("no_trade_reasons"))
        d["no_trade_hint"] = _clean_str(d.get("no_trade_hint") or "")

    return d


def _normalize_side(d: dict) -> None:
    raw = (d.get("side") or d.get("direction") or "").strip().lower()
    m = {
        "buy": "long",
        "sell": "short",
        "long": "long",
        "short": "short",
        "l": "long",
        "s": "short",
    }
    if raw in m:
        d["side"] = m[raw]
        return
    tr = (d.get("technical_rationale") or "").lower()
    if " short" in tr and " long" not in tr:
        d["side"] = "short"
    elif " long" in tr and " short" not in tr:
        d["side"] = "long"
    else:
        d.setdefault("side", "long")


def _normalize_entry_mode(d: dict) -> None:
    em = (d.get("entry_mode") or "").strip().lower()
    if em in {"market", "now", "mkt"}:
        flags = [f.lower() for f in (d.get("risk_flags") or [])]
        overbought_hint = any(
            k in (d.get("technical_rationale", "").lower())
            for k in ["перекуп", "overbought"]
        )
        weak_htf = any("weak_htf_rsi" in f or "htf" in f for f in flags)
        high_conf = d.get("confidence") == "High"
        if high_conf and not (overbought_hint or weak_htf):
            d["entry_mode"] = "now"
            d.setdefault("warnings", []).append("market_entry_high_conf")
        else:
            d["entry_mode"] = "limit"
            d.setdefault("warnings", []).append("market_downgraded_to_limit")
    elif em in {"wait_confirm", "wait-confirm", "confirm", "wc"}:
        d["entry_mode"] = "wait_confirm"
    elif em in {"limit", "lim", "lmt"}:
        d["entry_mode"] = "limit"
    else:
        d.setdefault("entry_mode", "limit")


def _resolve_lessons_path():
    _env_path = os.getenv("LLM_LESSONS_FILE")
    if not _env_path:
        return None
    q = _pl.Path(_env_path)
    if not q.is_absolute():
        q = (_pl.Path(__file__).resolve().parent / q).resolve()
    return q


def load_lessons_old():
    """
    Возвращает (text, count, path_str)
    """
    q = _resolve_lessons_path()
    if not q:
        return ("", 0, "")
    try:
        if not q.exists():
            return ("", 0, str(q))
        raw = q.read_text(encoding="utf-8")
    except Exception:
        return ("", 0, str(q))
    lines = [
        ln
        for ln in raw.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    body = "\n".join(lines).strip()
    if not body:
        return ("", 0, str(q))
    text = "\n# [LESSONS]\n" + body + "\n"
    try:
        if "AUTO_LESSONS" not in raw:
            _ap = (
                _pl.Path(__file__).resolve().parent
                / "auto_feedback/lessons/Auto_Lessons.md"
            ).resolve()
            if _ap.exists():
                _auto = _ap.read_text(encoding="utf-8").strip()
                if _auto:
                    body2 = (body + "\n" + _auto).strip()
                    text = "\n# [LESSONS]\n" + body2 + "\n"
                    lines = [
                        ln
                        for ln in body2.splitlines()
                        if ln.strip() and not ln.lstrip().startswith("#")
                    ]
                    return (text, len(lines), str(q))
    except Exception:
        pass
    return (text, len(lines), str(q))


# ---------------- Helpers ----------------
def snapshot_from_status() -> str:
    """Возвращаем текст от ./status (или ./status --for-llm), без падений при ошибке."""
    try:
        cmd = (
            "cd ~/llm-signal && ./status --for-llm 2>/dev/null || ./status 2>/dev/null"
        )
        res = subprocess.run(
            ["bash", "-lc", cmd], capture_output=True, text=True, timeout=30
        )
        return res.stdout.strip()
    except Exception:
        return ""


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def current_msk() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")


# ---- тикеры с last и 24h % ----
def _fetch_ticker(ex, sym):
    t = ex.fetch_ticker(sym)
    last = t.get("last")
    if last is None:
        bid, ask = t.get("bid"), t.get("ask")
        if bid and ask:
            last = (bid + ask) / 2
    change = t.get("percentage")
    return (
        float(last) if last is not None else None,
        float(change) if change is not None else None,
    )


def get_pair_ticker(sym: str):
    bybit = ccxt.bybit()
    binance = ccxt.binance()
    for ex in (bybit, binance):
        try:
            last, change = _fetch_ticker(ex, sym)
            if last is not None:
                return {"last": last, "change": change}
        except Exception:
            pass
    return {"last": None, "change": None}


def get_pool_snapshot() -> dict:
    with open("pool.json", "r", encoding="utf-8") as f:
        pool = json.load(f)["pool"]
    out = {}
    for sym in pool:
        out[sym] = get_pair_ticker(sym)
    return out


# ----- NEWS helpers -----
def get_news_block(hours: int = 12) -> str:
    """Возвращает текстовый блок NEWS из локального news_snapshot.py (без падений при ошибке)."""
    try:
        news_txt = subprocess.run(
            ["bash", "-lc", f"./news_snapshot.py {hours}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except Exception:
        news_txt = ""
    if not news_txt:
        news_txt = "— новости недоступны"
    return (
        "\n==========================\n"
        "=== [NEWS] ===\n" + news_txt + "\n"
        "==========================\n"
    )


def build_news_focus(symbol: str, news_block: str) -> str:
    """Фильтрация до 3 строк по тикеру/макро-триггерам."""
    symbol_root = symbol.split("/")[0].upper() if symbol else ""
    lines = [
        ln.lstrip("- ").strip()
        for ln in news_block.splitlines()
        if ln.strip().startswith("- ")
    ]
    picked = []
    for ln in lines:
        u = ln.upper()
        if (symbol_root and symbol_root in u) or any(
            k in u
            for k in (
                "ETF",
                "SEC",
                "FED",
                "MACRO",
                "INFLATION",
                "LIQUIDATION",
                "LIQUIDATIONS",
            )
        ):
            picked.append(ln)
        if len(picked) >= 3:
            break
    return "\n".join(picked)


def build_entries(d: dict) -> None:
    """Формирует три профиля входа (aggressive / neutral / conservative)."""
    try:
        price = float(d.get("price") or 0.0)
        er = d.get("entry_range") or {}
        emin = er.get("min")
        emax = er.get("max")
        if not price or emin is None or emax is None:
            return

        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if side not in ("long", "short"):
            return

        c_min = float(emin)
        c_max = float(emax)
        if c_max < c_min:
            c_min, c_max = c_max, c_min
        c_mid = (c_min + c_max) / 2.0
        width = c_max - c_min
        if width <= 0:
            width = max(abs(c_mid) * 0.001, 0.001)

        entries: dict = {
            "aggressive": {"enabled": False},
            "neutral": {"enabled": False},
            "conservative": {"enabled": False},
        }

        entries["conservative"] = {
            "enabled": True,
            "range": {"min": round(c_min, 6), "max": round(c_max, 6)},
            "entry_mode": d.get("entry_mode") or "limit",
            "position_size_hint": "0.75x",
            "comment": "Глубокий откат, более безопасный вход.",
        }

        if side == "long" and price <= c_mid:
            d["entries"] = entries
            return
        if side == "short" and price >= c_mid:
            d["entries"] = entries
            return

        if side == "long":
            d_abs = price - c_mid
            n_mid = c_mid + 0.5 * d_abs
            n_max = min(price, n_mid + width / 2.0)
            n_min = n_max - width / 2.0

            a_mid = c_mid + 0.8 * d_abs
            a_max = min(price, a_mid + width * 0.3)
            a_min = a_max - width * 0.3
        else:
            d_abs = c_mid - price
            n_mid = c_mid - 0.5 * d_abs
            n_min = max(price, n_mid - width / 2.0)
            n_max = n_min + width / 2.0

            a_mid = c_mid - 0.8 * d_abs
            a_min = max(price, a_mid - width * 0.3)
            a_max = a_min + width * 0.3

        entries["neutral"] = {
            "enabled": True,
            "range": {
                "min": round(n_min, 6),
                "max": round(n_max, 6),
            },
            "entry_mode": d.get("entry_mode") or "limit",
            "position_size_hint": "1.0x",
            "comment": "Основной рабочий вход, баланс вероятности и риска.",
        }

        if abs(a_max - a_min) > 0:
            entries["aggressive"] = {
                "enabled": True,
                "range": {
                    "min": round(a_min, 6),
                    "max": round(a_max, 6),
                },
                "entry_mode": (
                    "market_or_limit"
                    if (d.get("entry_mode") or "").lower() == "market"
                    else d.get("entry_mode") or "limit"
                ),
                "position_size_hint": "0.5x",
                "comment": "Более близкий к текущей цене вход, только по тренду, с повышенным риском.",
            }

        ema_guard = d.get("ema_guard") or {}
        state = (ema_guard.get("state") or "unknown").lower()
        if state == "between":
            entries["aggressive"]["enabled"] = False
        if state == "below_both" and side == "long":
            entries["aggressive"]["enabled"] = False
        if state == "above_both" and side == "short":
            entries["aggressive"]["enabled"] = False

        d["entry_range"] = entries["neutral"]["range"]
        d["entries"] = entries
    except Exception:
        return


def apply_ema_exhale_filter(d: dict) -> None:
    """
    EMA-filter v1 (phase guard):
    - различает фазы impulse → exhale → continuation (+ between как неопределённость)
    - не меняет direction/side
    - не трогает RR/SL/TP и ТВХ-валидацию
    - работает через ограничения входов в режимах (и no_trade только там, где нужно)
    """
    warnings = d.setdefault("warnings", [])

    try:
        price = float(d.get("price") or 0.0)
    except Exception:
        price = 0.0

    side = (d.get("side") or d.get("direction") or "").strip().lower()
    mode = normalize_mode(d.get("mode"))

    em15 = d.get("ema20_m15")
    em1h = d.get("ema20_h1")
    try:
        ema20_m15 = float(em15) if em15 is not None else None
    except Exception:
        ema20_m15 = None
    try:
        ema20_h1 = float(em1h) if em1h is not None else None
    except Exception:
        ema20_h1 = None

    fan_state = str(d.get("ema_fan_m15_state") or "").strip().lower()
    if fan_state not in {"bull", "bear", "mixed"}:
        fan_state = "unknown"

    entries = d.get("entries")
    if not isinstance(entries, dict):
        entries = None

    def _get_entry_range() -> dict:
        er = d.get("entry_range")
        if isinstance(er, dict) and ("min" in er or "max" in er):
            return er
        if entries:
            nr = (entries.get("neutral") or {}).get("range")
            if isinstance(nr, dict) and ("min" in nr or "max" in nr):
                return nr
        return {}

    def _disable_aggressive_entry() -> None:
        if not entries:
            return
        agg = entries.get("aggressive")
        if isinstance(agg, dict):
            agg["enabled"] = False

    def _tighten_conservative_size_hint() -> None:
        if not entries:
            return
        cons = entries.get("conservative")
        if isinstance(cons, dict):
            cons["position_size_hint"] = "0.5x"

    try:
        entry_ref = float(d.get("entry_price_neutral")) if d.get("entry_price_neutral") is not None else None
    except Exception:
        entry_ref = None

    try:
        er = _get_entry_range()
        entry_min = er.get("min")
        entry_max = er.get("max")
        if (
            (entry_ref is None)
            and entry_min is not None
            and entry_max is not None
        ):
            a = float(entry_min)
            b = float(entry_max)
            if b < a:
                a, b = b, a
            entry_mid = (a + b) / 2.0
            if entry_mid:
                entry_ref = entry_mid
    except Exception:
        pass

    if entry_ref is None or not entry_ref:
        if fan_state == "mixed":
            _disable_aggressive_entry()
            if "phase_between" not in warnings:
                warnings.append("phase_between")
        return

    try:
        if ema20_m15 is not None:
            if abs(entry_ref - ema20_m15) / abs(entry_ref) > 0.004:
                if "entry_not_anchored_to_ema20_m15" not in warnings:
                    warnings.append("entry_not_anchored_to_ema20_m15")
    except Exception:
        pass

    dist_entry_m15 = None
    dist_entry_h1 = None
    try:
        if side in {"long", "short"} and ema20_m15 is not None:
            dist_entry_m15 = (entry_ref - ema20_m15) / entry_ref
    except Exception:
        dist_entry_m15 = None
    try:
        if side in {"long", "short"} and ema20_h1 is not None:
            dist_entry_h1 = (entry_ref - ema20_h1) / entry_ref
    except Exception:
        dist_entry_h1 = None

    chasing_impulse = False
    try:
        if price:
            er = _get_entry_range()
            entry_min = er.get("min")
            entry_max = er.get("max")
            if entry_min is not None and entry_max is not None:
                a = float(entry_min)
                b = float(entry_max)
                if b < a:
                    a, b = b, a
                # если entry_range пересекает текущую цену — это догоняющий вход → трактуем как IMPULSE
                if side == "long" and b >= price:
                    chasing_impulse = True
                if side == "short" and a <= price:
                    chasing_impulse = True
    except Exception:
        chasing_impulse = False

    is_exhale = bool(dist_entry_m15 is not None and abs(dist_entry_m15) <= 0.004)
    is_impulse = bool(
        chasing_impulse
        or (
            fan_state in {"bull", "bear"}
            and dist_entry_m15 is not None
            and abs(dist_entry_m15) > 0.006
        )
    )

    entry_between_emas = False
    if ema20_m15 is not None and ema20_h1 is not None:
        lo = min(ema20_m15, ema20_h1)
        hi = max(ema20_m15, ema20_h1)
        if lo <= entry_ref <= hi:
            entry_between_emas = True
            if "ema_between_m15_h1" not in warnings:
                warnings.append("ema_between_m15_h1")
    is_between = bool(fan_state == "mixed" or entry_between_emas)

    if is_impulse:
        if "impulse_no_exhale" not in warnings:
            warnings.append("impulse_no_exhale")
    elif is_between:
        if "phase_between" not in warnings:
            warnings.append("phase_between")

    # EXHALE (правильная фаза): фильтр не вмешивается
    if is_exhale:
        return

    def _set_waiting_confirmation(hint: str = "ожидание подтверждения структуры") -> None:
        was_no_trade = bool(d.get("no_trade"))
        d["no_trade"] = True
        if not was_no_trade:
            d["no_trade_reasons"] = ["waiting_confirmation"]
            d["no_trade_hint"] = hint
            return
        reasons = d.get("no_trade_reasons")
        if not isinstance(reasons, list):
            reasons = []
        if "waiting_confirmation" not in reasons:
            reasons.append("waiting_confirmation")
        d["no_trade_reasons"] = reasons
        if not (d.get("no_trade_hint") or "").strip():
            d["no_trade_hint"] = hint

    # IMPULSE: догоняющий вход без выдоха
    if is_impulse:
        if mode == "aggressive":
            _disable_aggressive_entry()
            _tighten_conservative_size_hint()
            return
        if mode in {"neutral", "conservative"}:
            _set_waiting_confirmation("ожидание подтверждения структуры")
            return

    # BETWEEN: неопределённость / высокая вероятность пилы
    if is_between:
        _disable_aggressive_entry()
        if mode == "conservative":
            _set_waiting_confirmation("ожидание подтверждения структуры")
        return

    return


def _round_price(val):
    try:
        v = float(val)
    except Exception:
        return None
    av = abs(v)
    prec = 2 if av >= 1 else (4 if av >= 0.01 else 6)
    return round(v, prec)


def apply_direction_guard(d: dict) -> None:
    """
    Мягкий guard направления по EMA20(M15/H1): добавляет предупреждения,
    но не меняет side/direction.
    """
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return

    emode = (d.get("entry_mode") or "").strip().lower()
    if emode in ("now", "market"):
        return

    try:
        price = float(d.get("price") or 0.0)
    except Exception:
        return

    em15 = d.get("ema20_m15")
    em1h = d.get("ema20_h1")

    # если нет чисел — ничего не делаем
    if not (
        price
        and isinstance(em15, (int, float))
        and isinstance(em1h, (int, float))
    ):
        return

    if price < em15 and price < em1h and side == "long":
        warnings = d.setdefault("warnings", [])
        if "dir_guard_forced_short_by_ema" not in warnings:
            warnings.append("dir_guard_forced_short_by_ema")
        ema_guard = d.get("ema_guard")
        if isinstance(ema_guard, dict):
            note = ema_guard.get("note") or ema_guard.get("comment")
            if not note:
                ema_guard["comment"] = "long_against_ema_downtrend"


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and x == x


def apply_ema_blocks_and_derivatives(d: dict, symbol: str | None) -> None:
    periods = (9, 12, 20, 50, 200)
    if not symbol:
        d.setdefault("ema_m15", {f"ema{p}": None for p in periods})
        d.setdefault("ema_h1", {f"ema{p}": None for p in periods})
        d.setdefault("ema_fan_m15_state", "mixed")
        d.setdefault("ema_fan_h1_state", "mixed")
        d.setdefault("pivot_ema_hint_by_mode", "ema20")
        return

    ema_m15 = d.get("ema_m15") if isinstance(d.get("ema_m15"), dict) else {}
    ema_h1 = d.get("ema_h1") if isinstance(d.get("ema_h1"), dict) else {}

    for p in periods:
        k = f"ema{p}"
        if k not in ema_m15 or ema_m15.get(k) is None:
            if p == 20 and _is_num(d.get("ema20_m15")):
                ema_m15[k] = float(d["ema20_m15"])
            else:
                ema_m15[k] = get_ema(p, "m15", symbol=symbol)
        if k not in ema_h1 or ema_h1.get(k) is None:
            if p == 20 and _is_num(d.get("ema20_h1")):
                ema_h1[k] = float(d["ema20_h1"])
            else:
                ema_h1[k] = get_ema(p, "h1", symbol=symbol)

    d["ema_m15"] = {f"ema{p}": ema_m15.get(f"ema{p}") for p in periods}
    d["ema_h1"] = {f"ema{p}": ema_h1.get(f"ema{p}") for p in periods}

    def fan_state(ema_block: dict) -> str:
        e9 = ema_block.get("ema9")
        e12 = ema_block.get("ema12")
        e20 = ema_block.get("ema20")
        e50 = ema_block.get("ema50")
        if not all(_is_num(x) for x in (e9, e12, e20, e50)):
            return "mixed"
        if e9 > e12 > e20 > e50:
            return "bull"
        if e9 < e12 < e20 < e50:
            return "bear"
        return "mixed"

    d["ema_fan_m15_state"] = fan_state(d["ema_m15"])
    d["ema_fan_h1_state"] = fan_state(d["ema_h1"])

    mode = normalize_mode(d.get("mode"))
    d["pivot_ema_hint_by_mode"] = (
        "ema9_or_ema12"
        if mode == "aggressive"
        else ("ema50" if mode == "conservative" else "ema20")
    )


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


def apply_entry_prices_from_ranges(d: dict) -> None:
    neutral_mid = _mid_from_range(d.get("entry_range"))

    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    agg_mid = _mid_from_range(((entries.get("aggressive") or {}).get("range")))
    cons_mid = _mid_from_range(((entries.get("conservative") or {}).get("range")))

    if neutral_mid is None:
        neutral_mid = _mid_from_range(((entries.get("neutral") or {}).get("range")))

    if agg_mid is None:
        agg_mid = neutral_mid
    if cons_mid is None:
        cons_mid = neutral_mid

    d["entry_price_neutral"] = _round_price(neutral_mid)
    d["entry_price_aggressive"] = _round_price(agg_mid)
    d["entry_price_conservative"] = _round_price(cons_mid)


def validate_or_fallback_tvh_by_mode(d: dict) -> dict:
    """
    Гарантирует наличие sl_by_mode/tp_by_mode/rr_by_mode/exit_plan_by_mode,
    если no_trade == false. Использует LLM-значения при корректности, иначе — fallback
    от entry_price_<mode> по фиксированным правилам.
    """

    def _as_float(x):
        try:
            v = float(x)
        except Exception:
            return None
        if not math.isfinite(v):
            return None
        return v

    if bool(d.get("no_trade")):
        return d

    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return d
    is_long = side == "long"

    def _entry_price_by_mode(mode: str) -> float | None:
        k = f"entry_price_{mode}"
        v = _as_float(d.get(k))
        if v is not None:
            return v

        entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
        bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}
        mid = _mid_from_range(bucket.get("range"))
        if mid is None:
            mid = _mid_from_range(d.get("entry_range"))
        if mid is not None:
            d[k] = _round_price(mid)
            return float(mid)

        px = _as_float(d.get("price"))
        if px is not None:
            d[k] = _round_price(px)
            return float(px)
        return None

    def _valid_sl(entry: float, sl: float) -> bool:
        return sl < entry if is_long else sl > entry

    def _valid_tvh(entry: float, tvh: float) -> bool:
        return tvh > entry if is_long else tvh < entry

    def _rr(entry: float, sl: float, target: float) -> float | None:
        risk = abs(entry - sl)
        if not risk:
            return None
        return abs(target - entry) / risk

    sl_in = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
    tp_in = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
    ep_in = d.get("exit_plan_by_mode") if isinstance(d.get("exit_plan_by_mode"), dict) else {}

    # exit plan per mode
    plan_parts = [
        str(d.get("take_profit_rules") or "").strip(),
        str(d.get("break_even_rule") or "").strip(),
    ]
    plan_default = " ".join(p for p in plan_parts if p).strip()
    exit_plan_by_mode: dict[str, str] = {
        m: str(ep_in.get(m) or plan_default).strip() for m in VALID_MODES
    }

    rr_min_by_mode = {"aggressive": 1.0, "neutral": 1.5, "conservative": 2.0}
    active_mode = normalize_mode(d.get("mode"))
    rr_ok_for_active_mode = True

    sl_by_mode: dict[str, float] = {}
    tp_by_mode: dict[str, dict] = {}
    rr_by_mode: dict[str, float] = {}

    for mode in ("aggressive", "neutral", "conservative"):
        entry = _entry_price_by_mode(mode)
        if entry is None:
            if mode == active_mode:
                rr_ok_for_active_mode = False
            continue

        # SL
        sl_val = _as_float(sl_in.get(mode))
        if sl_val is None or not _valid_sl(entry, sl_val):
            if is_long:
                sl_val = entry * (0.985 if mode == "conservative" else 0.99)
            else:
                sl_val = entry * (1.015 if mode == "conservative" else 1.01)
        sl_val = float(_round_price(sl_val) if _round_price(sl_val) is not None else sl_val)
        sl_by_mode[mode] = sl_val

        bucket_in = tp_in.get(mode)
        bucket_in = bucket_in if isinstance(bucket_in, dict) else {}

        def _pick_num(*keys: str) -> float | None:
            for k in keys:
                if k in bucket_in:
                    v = _as_float(bucket_in.get(k))
                    if v is not None:
                        return v
            return None

        out_bucket: dict = {}
        if mode == "aggressive":
            tvh1 = _pick_num("tvh1", "tp1")
            tvh2 = _pick_num("tvh2", "tp2")
            tvh3 = _pick_num("tvh3", "tp3")
            if tvh1 is None or not _valid_tvh(entry, tvh1):
                tvh1 = entry * (1.005 if is_long else 0.995)
            if tvh2 is None or not _valid_tvh(entry, tvh2):
                tvh2 = entry * (1.01 if is_long else 0.99)
            if tvh3 is None or not _valid_tvh(entry, tvh3):
                tvh3 = entry * (1.02 if is_long else 0.98)

            tvh1 = float(_round_price(tvh1) if _round_price(tvh1) is not None else tvh1)
            tvh2 = float(_round_price(tvh2) if _round_price(tvh2) is not None else tvh2)
            tvh3 = float(_round_price(tvh3) if _round_price(tvh3) is not None else tvh3)
            if is_long and not (tvh1 < tvh2 < tvh3):
                tvh1 = float(_round_price(entry * 1.005) or (entry * 1.005))
                tvh2 = float(_round_price(entry * 1.01) or (entry * 1.01))
                tvh3 = float(_round_price(entry * 1.02) or (entry * 1.02))
            if (not is_long) and not (tvh1 > tvh2 > tvh3):
                tvh1 = float(_round_price(entry * 0.995) or (entry * 0.995))
                tvh2 = float(_round_price(entry * 0.99) or (entry * 0.99))
                tvh3 = float(_round_price(entry * 0.98) or (entry * 0.98))
            out_bucket = {"tvh1": tvh1, "tvh2": tvh2, "tvh3": tvh3}

        elif mode == "neutral":
            tvh1 = _pick_num("tvh1", "tp1")
            tvh2 = _pick_num("tvh2", "tp2")
            if tvh1 is None or not _valid_tvh(entry, tvh1):
                tvh1 = entry * (1.01 if is_long else 0.99)
            if tvh2 is None or not _valid_tvh(entry, tvh2):
                tvh2 = entry * (1.02 if is_long else 0.98)
            tvh1 = float(_round_price(tvh1) if _round_price(tvh1) is not None else tvh1)
            tvh2 = float(_round_price(tvh2) if _round_price(tvh2) is not None else tvh2)
            if is_long and not (tvh1 < tvh2):
                tvh1 = float(_round_price(entry * 1.01) or (entry * 1.01))
                tvh2 = float(_round_price(entry * 1.02) or (entry * 1.02))
            if (not is_long) and not (tvh1 > tvh2):
                tvh1 = float(_round_price(entry * 0.99) or (entry * 0.99))
                tvh2 = float(_round_price(entry * 0.98) or (entry * 0.98))
            out_bucket = {"tvh1": tvh1, "tvh2": tvh2}

        else:  # conservative
            tvh1 = _pick_num("tvh1", "tp1")
            tvh2_or_trail = bucket_in.get("tvh2_or_trail")
            if isinstance(tvh2_or_trail, str) and tvh2_or_trail.strip().lower() == "trail":
                tvh2_or_trail = "trail"
            else:
                tvh2_or_trail = _as_float(tvh2_or_trail)
                if tvh2_or_trail is not None and not _valid_tvh(entry, tvh2_or_trail):
                    tvh2_or_trail = None

            if tvh1 is None or not _valid_tvh(entry, tvh1):
                tvh1 = entry * (1.02 if is_long else 0.98)
            tvh1 = float(_round_price(tvh1) if _round_price(tvh1) is not None else tvh1)

            if tvh2_or_trail is None:
                tvh2_or_trail = "trail"
            elif isinstance(tvh2_or_trail, (int, float)):
                tvh2_or_trail = float(_round_price(tvh2_or_trail) if _round_price(tvh2_or_trail) is not None else tvh2_or_trail)
            out_bucket = {"tvh1": tvh1, "tvh2_or_trail": tvh2_or_trail}

        tp_by_mode[mode] = out_bucket

        # RR per mode: на дальнюю фиксированную цель (TVH3/TVH2/TVH1)
        rr_target = None
        if mode == "aggressive":
            rr_target = _as_float(out_bucket.get("tvh3")) or _as_float(out_bucket.get("tvh2")) or _as_float(out_bucket.get("tvh1"))
        elif mode == "neutral":
            rr_target = _as_float(out_bucket.get("tvh2")) or _as_float(out_bucket.get("tvh1"))
        else:
            rr_target = _as_float(out_bucket.get("tvh2_or_trail")) or _as_float(out_bucket.get("tvh1"))

        rr_val = _rr(entry, sl_val, rr_target) if rr_target is not None else None
        rr_by_mode[mode] = float(round(rr_val, 3)) if rr_val is not None else 0.0

        if mode == active_mode and (rr_val is None or rr_val < rr_min_by_mode[mode]):
            rr_ok_for_active_mode = False

    d["sl_by_mode"] = sl_by_mode
    d["tp_by_mode"] = tp_by_mode
    d["rr_by_mode"] = rr_by_mode
    d["exit_plan_by_mode"] = exit_plan_by_mode

    if not rr_ok_for_active_mode:
        d["no_trade"] = True
        reasons = d.get("no_trade_reasons")
        if not isinstance(reasons, list):
            reasons = []
        if "недостаточный RR для входа" not in reasons:
            reasons.append("недостаточный RR для входа")
        d["no_trade_reasons"] = reasons
        if not (d.get("no_trade_hint") or "").strip():
            d["no_trade_hint"] = "недостаточный RR для входа"

    return d


def finalize_signal(data: dict, hints: dict | None = None, *, fetch_price: bool = True) -> dict:
    hints = hints or {}
    d = data or {}

    if hints.get("symbol"):
        d["symbol"] = hints["symbol"]
    if hints.get("time_msk"):
        d["time_msk"] = hints["time_msk"]
    if "price" in hints and hints["price"] is not None:
        d["price"] = hints["price"]
    if "mode" in hints:
        mode_val = hints.get("mode")
    elif "mode" in d:
        mode_val = d.get("mode")
    else:
        mode_val = "neutral"
    d["mode"] = normalize_mode(mode_val)

    d.setdefault("warnings", [])
    if not d.get("time_msk"):
        d["time_msk"] = current_msk()

    symbol = d.get("symbol")
    if fetch_price and symbol and not d.get("price"):
        try:
            ticker = get_pair_ticker(symbol)
            rounded = _round_price(ticker.get("last"))
            if rounded is not None:
                d["price"] = rounded
        except Exception:
            pass

    sym_for_ema = hints.get("symbol") or symbol
    try:
        if "ema20_m15" not in d or not d["ema20_m15"]:
            d["ema20_m15"] = hints.get("ema20_m15") or (
                get_ema20_m15(sym_for_ema) if sym_for_ema else None
            )
        if "ema20_h1" not in d or not d["ema20_h1"]:
            d["ema20_h1"] = hints.get("ema20_h1") or (
                get_ema20_h1(sym_for_ema) if sym_for_ema else None
            )
    except Exception:
        pass

    _normalize_side(d)
    _normalize_entry_mode(d)
    ensure_defaults(d)

    try:
        _price = float(d.get("price") or 0.0)
        _ema15 = d.get("ema20_m15")
        _ema1h = d.get("ema20_h1")
        _state = "unknown"
        if _price and _ema15 and _ema1h:
            if _price > _ema15 and _price > _ema1h:
                _state = "above_both"
            elif _price < _ema15 and _price < _ema1h:
                _state = "below_both"
            else:
                _state = "between"
        d.setdefault(
            "ema_guard",
            {
                "ema20_m15": _ema15,
                "ema20_h1": _ema1h,
                "state": _state,
            },
        )
    except Exception:
        d.setdefault(
            "ema_guard",
            {
                "ema20_m15": None,
                "ema20_h1": None,
                "state": "unknown",
            },
        )

    apply_direction_guard(d)

    d.setdefault(
        "day_mid_context",
        {
            "day_bias": None,
            "mid_bias": None,
            "notes": None,
        },
    )
    d.setdefault(
        "adx_guard",
        {
            "m15": None,
            "strength": "unknown",
        },
    )

    _normalize_side(d)
    _normalize_entry_mode(d)
    ensure_defaults(d)

    d.setdefault("no_trade", False)
    d.setdefault("no_trade_reasons", [])
    d.setdefault("no_trade_hint", "")
    d.setdefault("max_valid_minutes", 90)

    build_entries(d)

    try:
        apply_ema_blocks_and_derivatives(d, sym_for_ema)
        apply_entry_prices_from_ranges(d)
    except Exception:
        pass

    apply_ema_exhale_filter(d)

    validate_or_fallback_tvh_by_mode(d)
    return normalize_no_trade(d)


def read_latest_report_text(root_dir: str, limit_chars: int = 2000) -> str:
    """
    Читает последний analysis_*.md из reports/day или reports/mid.
    Используется как мягкий контекст (вариант A).
    """
    try:
        base = BASE / "reports" / root_dir
        roots = sorted(base.glob("*"))
        if not roots:
            return ""
        d = roots[-1]
        an = sorted(d.glob("analysis_*.md"))
        if not an:
            return ""
        txt = an[-1].read_text(encoding="utf-8").strip()
        if limit_chars and len(txt) > limit_chars:
            return txt[-limit_chars:]
        return txt
    except Exception:
        return ""


# ---------------- Bootstrap ----------------
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("ERROR: OPENAI_API_KEY not set (put it in .env)", file=sys.stderr)
    sys.exit(1)
client = OpenAI(api_key=api_key)

ap = argparse.ArgumentParser()
ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
ap.add_argument("--params", default="params.json")
ap.add_argument("--symbol", default=None, help="Например: ETH/USDT (single-режим)")
ap.add_argument(
    "--multi", action="store_true", help="Мульти-анализ по пулу (анализ + JSON в конце)"
)
args = ap.parse_args()

# ================= MULTI MODE =================
if args.multi:
    system_prompt = read_file("prompt_analysis.txt")
    time_str = current_msk()

    params_payload = {}
    multi_hints = {}
    if os.path.exists(args.params):
        try:
            with open(args.params, "r", encoding="utf-8") as f:
                params_payload = json.load(f)
                multi_hints = params_payload.get("hints", {}) or {}
        except Exception:
            multi_hints = {}
    mode_raw = multi_hints.get("mode")
    mode = normalize_mode(mode_raw)

    pool_snapshot = get_pool_snapshot()
    snapshot = snapshot_from_status().strip()
    time_line = f"Время (МСК): {time_str}"
    if not snapshot:
        btc = get_pair_ticker("BTC/USDT")
        eth = get_pair_ticker("ETH/USDT")
        ctx = [time_line]
        ctx.append(
            "Контекст BTC/ETH (используй ТОЛЬКО эти значения; числовые уровни/диапазоны не придумывать):"
        )
        ctx.append(f"BTC/USDT: last={btc['last']}, change_24h={btc['change']}%")
        ctx.append(f"ETH/USDT: last={eth['last']}, change_24h={eth['change']}%")
        ctx.append("")
        ctx.append("Котировки альт-пула (USDT, 24h %):")
        for k, v in pool_snapshot.items():
            ctx.append(f"{k}: last={v['last']}, change_24h={v['change']}%")
        snapshot = "\n".join(ctx)
    elif time_line not in snapshot:
        snapshot = time_line + "\n" + snapshot

    btc_info = get_pair_ticker("BTC/USDT")
    eth_info = get_pair_ticker("ETH/USDT")
    btc_eth_line = (
        f"\n[BTC_ETH_24H]\n"
        f"BTC change_24h={btc_info['change']}%, ETH change_24h={eth_info['change']}% "
        f"(используй РОВНО эти проценты в поле market_context)\n"
    )

    btc_ch = btc_info.get("change")
    eth_ch = eth_info.get("change")
    risk_off = (
        btc_ch is not None
        and eth_ch is not None
        and btc_ch <= -2.5
        and eth_ch <= -3.0
    )

    risk_mode_hint = (
        "\n=== RISK MODE ===\n"
        "Сейчас строгий RISK-OFF: direction по умолчанию 'short'; "
        "лонг допускается лишь как редкий контртренд при явном развороте структуры (EMA/RSI/объём); "
        "если short-сетапа нет — лучше вернуть no_trade=true.\n"
        if risk_off
        else "\n=== RISK MODE ===\n"
        "Режим не risk-off: можно выбирать long/short, но избегай контртренда без подтверждения объёмом.\n"
    )
    mode_line = (
        f"Текущий режим: {mode}."
        if mode_raw in VALID_MODES
        else "Текущий режим: режим по умолчанию: neutral."
    )
    mode_block = (
        "=== TRADING MODE ===\n"
        f"{mode_line}\n"
        "- aggressive: больше входов, допускается контртренд, RR ≥ 1:1.\n"
        "- neutral: баланс фильтров и частоты, rare контртренд, RR ≥ 1:1.5.\n"
        "- conservative: только по тренду, строгие фильтры, RR ≥ 1:2.\n"
        "Выбор актива и сценария должен уважать режим: aggressive может допускать более рискованные идеи; "
        "conservative — только по тренду и с жёстким RR.\n"
    )

    try:
        with open("snapshot.txt", "w", encoding="utf-8") as f:
            f.write(snapshot + "\n")
    except Exception:
        pass
    print(
        "\n=== [SNAPSHOT ДЛЯ LLM] ===\n" + snapshot + "\n==========================\n"
    )

    news_block = get_news_block(12)
    time_msk = time_str

    day_txt = read_latest_report_text("day", limit_chars=2000)
    mid_txt = read_latest_report_text("mid", limit_chars=2000)

    pool_symbols = sorted(pool_snapshot.keys())
    pool_payload = {
        "time_msk_hint": time_msk,
        "snapshot_text": snapshot,
        "pool_symbols": pool_symbols,
        "pool_quotes": pool_snapshot,
        "btc_eth_change": {
            "btc_change_pct": btc_info.get("change"),
            "eth_change_pct": eth_info.get("change"),
        },
    }

    schema_hint = (
        risk_mode_hint
        + "\n=== JSON OUTPUT FORMAT (STRICT) ===\n"
        "Верни ОДИН JSON c двумя ключами: overview (list of paragraphs) и signal (объект).\n"
        "overview: 4–6 абзацев обзора по пулу, каждый абзац отдельной строкой массива.\n"
        "signal: объект с полями time_msk, symbol, price, direction, entry_range, sl, tp1, tp2, rr, "
        "take_profit_rules, break_even_rule, multi_tf_view, why_asset, news_context, market_context, "
        "validity_minutes, cancel_condition, technical_rationale, disclaimer, entry_mode, confidence, "
        "confirmation_rules, alt_entry_range, entries, no_trade, no_trade_reasons, no_trade_hint, "
        "max_valid_minutes, ema20_m15, ema20_h1, ema_guard, day_mid_context, adx_guard, "
        "tp_by_mode, rr_by_mode, exit_plan_by_mode.\n"
        "news_context: выбери 1–3 строки ПРЯМО из блока [NEWS] или NEWS_FOCUS и вставь их БЕЗ ИЗМЕНЕНИЙ, "
        "сохраняя префикс времени вида \"[YYYY-MM-DD HH:MM МСК]\" и тег [impact:…]. Нельзя удалять/менять "
        "timestamp/impact или переписывать заголовок. Допускается добавить пояснение только в конце через "
        "\" — ...\" после исходной строки.\n"
        f"symbol выбирай ТОЛЬКО из списка: {', '.join(pool_symbols)}.\n"
        f"time_msk установи ровно в это значение: {time_msk}.\n"
        "market_context ссылайся на проценты из блока [BTC_ETH_24H] как есть.\n"
        "\n=== EMA → TVH (TP) GUIDANCE (SOFT, FOR EXIT ONLY) ===\n"
        "EMA в этом шаге — ТОЛЬКО контекст для мышления при формировании ТВХ/TP (tp_by_mode) и логики выхода "
        "(exit_plan_by_mode). НЕ делай EMA обязательным правилом, НЕ вводи новых no-trade правил и НЕ меняй "
        "выбор направления (direction) из-за EMA.\n"
        "Используй уже существующие поля из входных данных/контекста:\n"
        "- ema_m15, ema_h1\n"
        "- ema_fan_m15_state, ema_fan_h1_state\n"
        "- pivot_ema_hint_by_mode\n"
        "Требование для tp_by_mode:\n"
        "- если EMA-fan расширен (bull/bear) и быстрые EMA (EMA9/12) заметно удалены от EMA20, цели могут быть шире "
        "(дальше TVH, трейлинг позже)\n"
        "- если EMA-fan схлопывается к EMA20 (быстрые EMA близко к EMA20 / состояние mixed / потеря импульса), цели ближе "
        "и трейлинг/BE раньше\n"
        "- ориентируйся на pivot_ema_hint_by_mode: aggressive → EMA9/EMA12, neutral → EMA20, conservative → EMA50\n"
        "Требование для exit_plan_by_mode:\n"
        "- для каждого режима (aggressive/neutral/conservative) добавь ОДНУ короткую фразу-пояснение, "
        "почему цели такие (без чисел EMA; используй формулировки «быстрая/средняя/медленная EMA», "
        "«fan расширяется/схлопывается», «трейлинг раньше/позже»).\n"
    )

    focus = build_news_focus("", news_block)

    user_prompt = (
        mode_block
        + "Работаешь в режиме MULTI/FULL. Сделай обзор по всему пулу (risk-on/off, лидеры/аутсайдеры), "
        "затем выбери один лучший актив для сигнала (вариант A).\n"
        "Сначала опиши пул (overview), затем составь signal по схеме. Не выдумывай уровни вне снапшота.\n"
        + btc_eth_line
        + schema_hint
        + "\n=== SNAPSHOT CONTEXT ===\n"
        + json.dumps(pool_payload, ensure_ascii=False, indent=2)
        + "\n"
        + news_block
    )

    if focus:
        user_prompt += "\nNEWS_FOCUS (top-3):\n" + focus + "\n"

    if day_txt or mid_txt:
        user_prompt += (
            "\n\n=== DAY/MID CONTEXT (SOFT, DO NOT OVERRIDE PRICE/EMA) ===\n"
            "Используй отчёты как мягкий фон: если совпадают — усиливай идею, если нет — отметь риск.\n"
        )
        if day_txt:
            user_prompt += "\n[DAY REPORT]\n" + day_txt + "\n"
        if mid_txt:
            user_prompt += "\n[MID REPORT]\n" + mid_txt + "\n"

    resp = client.chat.completions.create(
        model="gpt-5.1",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.75,
        top_p=0.9,
        seed=7,
    )
    content = resp.choices[0].message.content

    try:
        multi_payload_resp = json.loads(content)
    except Exception:
        print(content)
        sys.exit(1)

    overview_raw = multi_payload_resp.get("overview", [])
    if isinstance(overview_raw, str):
        overview_lines = [overview_raw.strip()]
    elif isinstance(overview_raw, list):
        overview_lines = [str(x).strip() for x in overview_raw if str(x).strip()]
    else:
        overview_lines = []

    signal_raw = multi_payload_resp.get("signal") or {}
    if not isinstance(signal_raw, dict):
        print(content)
        sys.exit(1)

    hints = {"time_msk": time_msk, "mode": mode}
    signal = finalize_signal(signal_raw, hints)
    signal["time_msk"] = time_msk

    if overview_lines:
        print("\n=== [POOL OVERVIEW] ===\n")
        print("\n\n".join(overview_lines))
        print("\n==========================\n")

    print(json.dumps(signal, ensure_ascii=False))
    sys.exit(0)

# ================= SINGLE MODE =================
system_prompt = read_file("prompt_system.txt")
anna_prompt = read_file("prompt_anna.txt")

payload: dict = {}
if os.path.exists(args.params):
    with open(args.params, "r", encoding="utf-8") as f:
        try:
            payload = json.load(f)
        except json.JSONDecodeError:
            print("ERROR: params.json is not valid JSON", file=sys.stderr)
            sys.exit(1)

payload.setdefault("hints", {})
if args.symbol:
    payload["hints"]["symbol"] = args.symbol
    # live price
    _sym = payload["hints"]["symbol"]
    try:
        _last = get_pair_ticker(_sym).get("last")
    except Exception:
        _last = None
        if _last is not None:
            try:
                payload["hints"]["price"] = _round_price(_last)
                payload["hints"]["price_source"] = "live"
            except Exception:
                pass

# time hint
payload["hints"]["time_msk"] = current_msk()

# EMA20 hints
_sym = payload["hints"].get("symbol")
payload["hints"]["ema20_m15"] = get_ema20_m15(_sym) if _sym else None
payload["hints"]["ema20_h1"] = get_ema20_h1(_sym) if _sym else None

btc_info = get_pair_ticker("BTC/USDT")
eth_info = get_pair_ticker("ETH/USDT")
btc_ch = btc_info.get("change")
eth_ch = eth_info.get("change")
risk_off = (
    btc_ch is not None
    and eth_ch is not None
    and btc_ch <= -2.5
    and eth_ch <= -3.0
)
risk_hint = (
    "СЕЙЧАС строгий RISK-OFF: BTC_change_24h и ETH_change_24h ≤ порогов. "
    'По умолчанию выбирай direction="short". '
    "Лонг допустим ТОЛЬКО как редкий контртренд от сильной поддержки при развороте EMA/RSI/объёма; "
    "если берёшь такой лонг — явно пометь countertrend=true и честно опиши риск. "
    "Если чистого short-сетапа нет — верни no_trade=true и объясни почему.\n"
    if risk_off
    else ""
)
mode_raw = payload["hints"].get("mode")
mode = normalize_mode(mode_raw)
payload["hints"]["mode"] = mode
mode_block = (
    "=== TRADING MODE ===\n"
    f"Текущий режим: {mode}.\n"
    "- aggressive: больше входов, допускается контртренд, RR ≥ 1:1.\n"
    "- neutral: баланс фильтров и частоты, rare контртренд, RR ≥ 1:1.5.\n"
    "- conservative: только по тренду, строгие фильтры, RR ≥ 1:2.\n"
)

# базовый user_prompt
base_user_prompt = (
    mode_block
    + risk_hint
    + "Сгенерируй один JSON по заданной схеме. "
    "Используй hints как обязательные значения; constraints — как жёсткие ограничения. "
    f"Поле time_msk установи РОВНО в это значение: {payload['hints']['time_msk']}. "
    "Поле price, если задано, используй РОВНО как задано. "
    "Если явных новостей нет (hints.news нет) — верни \"news_context\": [].\n"
    "Входные данные:\n" + json.dumps(payload, ensure_ascii=False)
)

# NEWS → в prompt
news_block = get_news_block(12)

# LESSONS отключены
lessons_text = ""

schema_single = (
    "\n=== JSON OUTPUT FORMAT (STRICT SINGLE) ===\n"
    "Верни ОДИН JSON-объект со следующими полями:\n"
    "- time_msk: строка 'dd.mm.yyyy, HH:MM' по МСК\n"
    "- symbol: строка вида 'TICKER/USDT'\n"
    "- price: число (текущая цена, не выдумывать)\n"
    "- direction: 'long' или 'short'\n"
    "- entry_range: {\"min\": number, \"max\": number} — диапазон входа по EMA20(M15)\n"
    "- sl: число (stop-loss)\n"
    "- tp1: число (первая цель)\n"
    "- tp2: число (вторая цель)\n"
    "- rr: число (отношение риск/прибыль по TP2)\n"
    "- tp_by_mode: объект целей ТВХ по режимам: aggressive/neutral/conservative (см. требования ниже)\n"
    "- rr_by_mode: объект RR по режимам: aggressive/neutral/conservative\n"
    "- exit_plan_by_mode: объект плана выхода по режимам: aggressive/neutral/conservative\n"
    "- take_profit_rules: строка с логикой фиксации прибыли\n"
    "- break_even_rule: строка с правилом перевода в безубыток\n"
    "- multi_tf_view: объект с ключами m5, m15, h1, h4, d1 — каждое значение короткая строка-описание структуры\n"
    "- why_asset: строка — почему выбран актив\n"
    "- news_context: массив строк из блока [NEWS]/NEWS_FOCUS (уже с префиксом даты и impact), без изменения текста\n"
    "- market_context: строка с использованием [BTC_ETH_24H]\n"
    "- validity_minutes: число (например, 90)\n"
    "- cancel_condition: строка — при каких условиях сетап отменяется\n"
    "- technical_rationale: строка — связное обоснование сетапа (можно длинное)\n"
    "- disclaimer: строка с дисклеймером\n"
    "- entry_mode: 'limit', 'now' или 'wait_confirm'\n"
    "- confidence: 'High' | 'Medium' | 'Low'\n"
    "- confirmation_rules: строка или массив строк с чек-листом подтверждения\n"
    "- alt_entry_range: {\"min\": number, \"max\": number} — альтернативная зона входа\n"
    "- entries: объект с тремя профилями входа (aggressive/neutral/conservative), как уже описано в коде\n"
    "- no_trade: true/false — разрешён ли вход\n"
    "- no_trade_reasons: список строк причин отказа\n"
    "- no_trade_hint: строка с кратким пояснением отказа, если no_trade=true\n"
    "- max_valid_minutes: число (обычно 90)\n"
    "- ema20_m15: число (если известно)\n"
    "- ema20_h1: число (если известно)\n"
    "- ema_guard: объект с EMA-контекстом (как в finalize_signal)\n"
    "- day_mid_context: объект с полями day_bias, mid_bias, notes\n"
    "- adx_guard: объект для силы тренда (может быть заглушкой)\n"
    "\n=== EMA → TVH (TP) GUIDANCE (SOFT, FOR EXIT ONLY) ===\n"
    "EMA — не жёсткое правило и не повод блокировать сигнал. Используй EMA ТОЛЬКО как ориентир глубины целей "
    "и логики выхода (tp_by_mode + exit_plan_by_mode). Direction/side не меняй из-за EMA.\n"
    "Используй уже существующие поля:\n"
    "- ema_m15, ema_h1\n"
    "- ema_fan_m15_state, ema_fan_h1_state\n"
    "- pivot_ema_hint_by_mode\n"
    "Требование для tp_by_mode:\n"
    "- если EMA-fan расширен (bull/bear) и быстрые EMA (EMA9/12) заметно удалены от EMA20 → цели шире, трейлинг позже\n"
    "- если EMA-fan схлопывается к EMA20 → цели ближе, трейлинг/BE раньше\n"
    "- pivot_ema_hint_by_mode: aggressive → EMA9/EMA12, neutral → EMA20, conservative → EMA50\n"
    "Требование для exit_plan_by_mode:\n"
    "- для каждого режима добавь ОДНУ короткую фразу, почему цели такие (без чисел EMA; "
    "используй «быстрая/средняя/медленная EMA», «fan расширяется/схлопывается»).\n"
    "Все поля должны быть заполнены; если данных нет — используй осмысленное значение (например, пустой массив/строку), но ключ обязательно присутствует.\n"
)

# финальный user_prompt для SINGLE
user_prompt = (
    schema_single
    + "\n"
    + (((lessons_text + "\n") if lessons_text else "") + base_user_prompt + news_block)
)

# стиль + формат symbol/time_msk
user_prompt += (
    "\n\n=== OUTPUT STYLE REQUIREMENTS ===\n"
    "- symbol: строго в формате TICKER/USDT из пула (например, LINK/USDT; НЕ LINKUSDT).\n"
    "- time_msk: формат ровно 'dd.mm.yyyy, HH:MM' по МСК.\n"
    "- news_context: выбери 1–3 строки прямо из блока [NEWS] или NEWS_FOCUS и вставь их БЕЗ ИЗМЕНЕНИЙ, "
    "сохранив timestamp вида \"[YYYY-MM-DD HH:MM МСК]\" и тег [impact:…]. Нельзя удалять/менять "
    "timestamp/impact или переписывать заголовок. Если нужно пояснение — добавь его только после "
    "исходной строки через \" — ...\". Используй только факты из [NEWS], не придумывай уровни/цифры.\n"
)

# NEWS_FOCUS c учётом тикера
focus = build_news_focus(payload.get("hints", {}).get("symbol", ""), news_block)
if focus:
    user_prompt += "\nNEWS_FOCUS (top-3):\n" + focus + "\n"

# мягкий контекст DAY/MID и для SINGLE (вариант A)
day_txt = read_latest_report_text("day", limit_chars=2000)
mid_txt = read_latest_report_text("mid", limit_chars=2000)
if day_txt or mid_txt:
    user_prompt += (
        "\n\n=== DAY/MID CONTEXT (SOFT, DO NOT OVERRIDE PRICE/EMA) ===\n"
        "DAY/MID-отчёты используй как фон: если они совпадают с текущей идеей — усиливай обоснование; "
        "если противоречат — честно укажи риск, но решение принимай по текущей структуре цены/EMA/объёма.\n"
    )
    if day_txt:
        user_prompt += "\n[DAY REPORT]\n" + day_txt + "\n"
    if mid_txt:
        user_prompt += "\n[MID REPORT]\n" + mid_txt + "\n"

# === LLM вызов (SINGLE) ===
resp = client.chat.completions.create(
    model="gpt-5.1",
    response_format={"type": "json_object"},
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": anna_prompt},
        {"role": "user", "content": user_prompt},
    ],
    temperature=0.4,
    top_p=0.85,
)
content = resp.choices[0].message.content

try:
    data = json.loads(content)
except Exception:
    print(content)
    sys.exit(0)

data = finalize_signal(data, payload.get("hints", {}))

print(json.dumps(data, ensure_ascii=False))
sys.exit(0)
