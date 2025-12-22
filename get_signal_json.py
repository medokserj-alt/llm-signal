#!/usr/bin/env python3
import os
import sys
import json
import math
import time
import re
import argparse
import copy
from datetime import datetime
from zoneinfo import ZoneInfo
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore[no-redef]
        return False

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover
    ccxt = None  # type: ignore[assignment]
import subprocess
from pathlib import Path
import pathlib as _pl

VALID_MODES = {"aggressive", "neutral", "conservative"}

# ---- Debug trace (diagnostics only; gated by env DEBUG_TRACE=1) ----
_DEBUG_TRACE_ENABLED = os.getenv("DEBUG_TRACE") == "1"
_DEBUG_TRACE: dict | None = {} if _DEBUG_TRACE_ENABLED else None
_DEBUG_TRACE_PATH = Path(__file__).resolve().parent / "logs" / "debug_trace.json"


def _debug_trace_reset() -> None:
    if _DEBUG_TRACE is None:
        return
    _DEBUG_TRACE.clear()


def _debug_trace_update_main_fields(d: dict | None) -> None:
    if _DEBUG_TRACE is None or not isinstance(d, dict):
        return
    for k in ("symbol", "time_msk", "price"):
        if k in d and d.get(k) is not None:
            _DEBUG_TRACE[k] = copy.deepcopy(d.get(k))


def _debug_trace_set_entry_range(field: str, d: dict | None) -> None:
    if _DEBUG_TRACE is None:
        return
    _debug_trace_update_main_fields(d if isinstance(d, dict) else None)
    try:
        _DEBUG_TRACE[field] = copy.deepcopy((d or {}).get("entry_range"))
    except Exception:
        try:
            _DEBUG_TRACE[field] = (d or {}).get("entry_range")
        except Exception:
            _DEBUG_TRACE[field] = None


def _debug_trace_write() -> None:
    if _DEBUG_TRACE is None:
        return
    try:
        _DEBUG_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DEBUG_TRACE_PATH.write_text(
            json.dumps(_DEBUG_TRACE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ---- EMA helpers (Bybit Futures-aligned) ----
_CLOSES_CACHE: dict[tuple[str, str, str], dict] = {}
_EXCHANGES: dict[str, object] = {}


def _normalize_timeframe(timeframe: str) -> str:
    tf = (timeframe or "").strip().lower()
    if tf in {"m15", "15m", "15"}:
        return "15m"
    if tf in {"h1", "1h", "60m", "60"}:
        return "1h"
    return tf


def _cache_ttl_seconds(timeframe: str) -> int:
    tf = _normalize_timeframe(timeframe)
    if tf == "15m":
        return 60
    if tf == "1h":
        return 180
    return 0


def _timeframe_ms(tf: str) -> int:
    tf = _normalize_timeframe(tf)
    if tf == "15m":
        return 15 * 60 * 1000
    if tf == "1h":
        return 60 * 60 * 1000
    return 0


def _normalize_bybit_swap_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    if not s:
        return s
    if ":" in s:
        return s
    # ccxt Bybit linear USDT perpetual обычно использует формат вида "BTC/USDT:USDT"
    if s.upper().endswith("/USDT"):
        return f"{s}:USDT"
    return s


def _get_exchange(name: str):
    ex = _EXCHANGES.get(name)
    if ex is not None:
        return ex
    if name == "bybit_swap":
        ex = ccxt.bybit(
            {
                "enableRateLimit": True,
                "options": {
                    "defaultType": "swap",  # важно: USDT Perpetual (linear swap), как в разделе "Фьючерсы"
                    "defaultSubType": "linear",
                },
            }
        )
    elif name == "binance_future":
        ex = ccxt.binance(
            {
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                },
            }
        )
    else:
        ex = ccxt.binance({"enableRateLimit": True})
    _EXCHANGES[name] = ex
    return ex


def _fetch_ohlcv_paginated(
    ex,
    *,
    symbol: str,
    timeframe: str,
    target_len: int,
    chunk_limit: int,
    since: int | None,
) -> list[list]:
    tf = _normalize_timeframe(timeframe)
    ms = _timeframe_ms(tf)
    if ms <= 0:
        return []

    out: list[list] = []
    seen_ts: set[int] = set()
    loops = 0
    max_loops = max(10, (target_len // max(1, chunk_limit)) + 5)

    next_since = since
    while len(out) < target_len and loops < max_loops:
        loops += 1
        try:
            remaining = target_len - len(out)
            lim = min(int(chunk_limit), int(remaining))
            ohlcv = ex.fetch_ohlcv(symbol, timeframe=tf, since=next_since, limit=lim)
        except Exception:
            break
        if not ohlcv:
            break

        # ccxt обычно возвращает по возрастанию времени; но мы защитимся от дублей/перестановок
        ohlcv_sorted = sorted(
            [c for c in ohlcv if isinstance(c, (list, tuple)) and len(c) >= 5],
            key=lambda c: int(c[0]),
        )
        appended_any = False
        last_ts = None
        for c in ohlcv_sorted:
            try:
                ts = int(c[0])
            except Exception:
                continue
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            out.append(list(c))
            appended_any = True
            last_ts = ts

        if not appended_any or last_ts is None:
            break

        # двигаем since на следующую свечу, чтобы не зациклиться на дублях
        next_since = last_ts + ms

    return out


def _fetch_closes_from_market(
    market: str,
    timeframe: str,
    *,
    symbol: str,
    limit: int,
    min_len: int,
) -> dict | None:
    tf = _normalize_timeframe(timeframe)
    ttl = _cache_ttl_seconds(tf)

    if market == "bybit_swap":
        ex_symbol = _normalize_bybit_swap_symbol(symbol)
    else:
        ex_symbol = (symbol or "").strip()

    cache_key = (ex_symbol, tf, market)
    cached = _CLOSES_CACHE.get(cache_key) or {}
    cached_closes = cached.get("closes")
    cached_fetched_at = cached.get("fetched_at")
    ttl_ok = False
    if ttl > 0 and isinstance(cached_fetched_at, (int, float)) and isinstance(cached_closes, list):
        try:
            age = time.time() - float(cached_fetched_at)
        except Exception:
            age = ttl + 1
        ttl_ok = age <= ttl
    if ttl_ok and len(cached_closes) >= min_len:
        return cached

    try:
        lim = int(limit)
    except Exception:
        return None
    if lim <= 0:
        return None
    try:
        min_required = int(min_len)
    except Exception:
        min_required = 1
    if min_required <= 0:
        min_required = 1

    ex = _get_exchange(market)
    ms = _timeframe_ms(tf)
    if ms <= 0:
        return None
    now_ms = int(time.time() * 1000)
    since = now_ms - (lim * ms)

    chunk = 200 if market == "bybit_swap" else 1000
    ohlcv = _fetch_ohlcv_paginated(
        ex,
        symbol=ex_symbol,
        timeframe=tf,
        target_len=lim,
        chunk_limit=chunk,
        since=since,
    )
    if not ohlcv:
        return None

    closes: list[float] = []
    last_candle_ts = None
    last_candle_raw: list | None = None
    for c in ohlcv:
        if len(c) < 5:
            continue
        if c[4] is None:
            continue
        try:
            closes.append(float(c[4]))
        except Exception:
            continue
        try:
            last_candle_ts = int(c[0])
        except Exception:
            pass
        last_candle_raw = list(c)

    if len(closes) < min_required:
        return None

    last_candle: dict | None = None
    if isinstance(last_candle_raw, list) and len(last_candle_raw) >= 5:
        try:
            last_candle = {
                "ts": int(last_candle_raw[0]),
                "open": float(last_candle_raw[1]),
                "high": float(last_candle_raw[2]),
                "low": float(last_candle_raw[3]),
                "close": float(last_candle_raw[4]),
                "volume": float(last_candle_raw[5]) if len(last_candle_raw) >= 6 and last_candle_raw[5] is not None else None,
            }
        except Exception:
            last_candle = None

    snap = {
        "market": market,
        "symbol": ex_symbol,
        "timeframe": tf,
        "last_candle_ts": last_candle_ts,
        "last_candle": last_candle,
        "ohlcv_count": len(ohlcv),
        "fetched_at": time.time(),
        "closes": closes,
    }
    _CLOSES_CACHE[cache_key] = snap
    return snap


def _ema_sma_seed(closes: list[float], period: int) -> float | None:
    if not closes:
        return None
    try:
        p = int(period)
    except Exception:
        return None
    if p <= 1 or len(closes) < p:
        return None

    # Инициализация EMA как SMA первых p значений (как на биржах/в терминалах)
    seed = sum(closes[:p]) / float(p)
    k = 2.0 / (float(p) + 1.0)
    ema = float(seed)
    for v in closes[p:]:
        ema = float(v) * k + ema * (1.0 - k)
    return ema


def get_ema(period: int, timeframe: str, *, symbol: str) -> float | None:
    """
    EMA, считающаяся "по-людски" (SMA seed + итерация) и по рынку Bybit USDT Perpetual (swap/linear),
    с длинным warm-up окном, чтобы совпадать по смыслу с индикаторами на бирже.
    """
    try:
        p = int(period)
    except Exception:
        return None
    if p <= 1:
        return None

    tf = _normalize_timeframe(timeframe)
    warmup_len = max(500, p * 20)

    # Источник: приоритет Bybit swap (USDT perpetual), затем fallback Binance (по возможности futures)
    for market in ("bybit_swap", "binance_future"):
        snap = _fetch_closes_from_market(
            market,
            tf,
            symbol=symbol,
            limit=warmup_len,
            min_len=p,
        )
        if not snap:
            continue
        closes = snap.get("closes") if isinstance(snap.get("closes"), list) else None
        if not closes:
            continue
        ema_val = _ema_sma_seed(closes, p)
        if ema_val is None:
            continue
        return round(float(ema_val), 6)

    return None


def get_ema20_m15(symbol: str):
    return get_ema(20, "15m", symbol=symbol)


def get_ema20_h1(symbol: str):
    return get_ema(20, "1h", symbol=symbol)


def get_ema_provenance(period: int, timeframe: str, *, symbol: str) -> dict:
    """
    Возвращает EMA и минимальный provenance для воспроизводимости:
    рынок/таймфрейм/кол-во свечей/последняя свеча/хвост close.
    """
    try:
        p = int(period)
    except Exception:
        p = 0
    tf = _normalize_timeframe(timeframe)

    out: dict = {
        "ema": None,
        "exchange": None,
        "market_type": None,
        "price_source": "last",
        "timeframe": tf,
        "candles_count": None,
        "last_candle": None,
        "closes_tail": None,
        "symbol": (symbol or "").strip() or None,
        "market": None,
    }

    if p <= 1:
        return out

    warmup_len = max(500, p * 20)
    for market in ("bybit_swap", "binance_future"):
        snap = _fetch_closes_from_market(
            market,
            tf,
            symbol=symbol,
            limit=warmup_len,
            min_len=p,
        )
        if not snap:
            continue
        closes = snap.get("closes") if isinstance(snap.get("closes"), list) else None
        if not closes:
            continue
        ema_val = _ema_sma_seed(closes, p)
        if ema_val is None:
            continue

        out["ema"] = round(float(ema_val), 6)
        out["market"] = market
        out["symbol"] = snap.get("symbol")
        out["timeframe"] = snap.get("timeframe") or tf
        out["candles_count"] = len(closes)
        out["last_candle"] = snap.get("last_candle")
        try:
            out["closes_tail"] = [float(x) for x in closes[-5:]]
        except Exception:
            out["closes_tail"] = None

        if market == "bybit_swap":
            out["exchange"] = "bybit"
            out["market_type"] = "linear_perp"
        elif market == "binance_future":
            out["exchange"] = "binance"
            out["market_type"] = "usdt_future"
        else:
            out["exchange"] = str(market)
            out["market_type"] = None

        return out

    return out


def debug_get_ema_snapshot(symbol: str) -> dict:
    """
    Диагностическая функция для ручного сравнения с Bybit Futures (USDT perpetual).
    Не вызывается автоматически.
    """
    sym = (symbol or "").strip()
    out = {"symbol": sym, "market": "bybit_swap", "m15": {}, "h1": {}}
    for tf, key in (("15m", "m15"), ("1h", "h1")):
        for p in (9, 12, 20):
            snap = _fetch_closes_from_market(
                "bybit_swap",
                tf,
                symbol=sym,
                limit=max(500, p * 20),
                min_len=p,
            )
            closes = snap.get("closes") if isinstance(snap, dict) else None
            ema_val = _ema_sma_seed(closes, p) if isinstance(closes, list) else None
            out[key][f"ema{p}"] = round(float(ema_val), 6) if ema_val is not None else None
    return out


def _to_float(x) -> float | None:
    try:
        v = float(x)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


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
    Нормализует no_trade-ветку (без “чистки” семантических маркеров).
    без изменения торговой логики (направление/EMA/фильтры).
    """
    def _clean_str(s: str) -> str:
        if not isinstance(s, str):
            return ""
        return s.strip()

    def _clean_list_str(xs) -> list[str]:
        if not isinstance(xs, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for it in xs:
            if not isinstance(it, str):
                continue
            s = it.strip()
            if not s:
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


def _msk_minutes_from_time_str(time_msk_val) -> int | None:
    """
    Возвращает минуты от полуночи по Москве.
    Поддерживает форматы:
    - "dd.mm.yyyy, HH:MM"
    - "HH:MM"
    """
    try:
        s = str(time_msk_val or "").strip()
    except Exception:
        s = ""
    if not s:
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


def _tw_clear_artifacts(d: dict) -> None:
    reasons = d.get("no_trade_reasons")
    if isinstance(reasons, list):
        d["no_trade_reasons"] = [
            r for r in reasons if not (isinstance(r, str) and "time_window" in r)
        ]
    warnings = d.get("warnings")
    if isinstance(warnings, list):
        d["warnings"] = [
            w for w in warnings if not (isinstance(w, str) and "time_window" in w)
        ]
    hint = d.get("no_trade_hint")
    if isinstance(hint, str):
        low = hint.lower()
        if "time_window" in low:
            d["no_trade_hint"] = ""


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
        if isinstance(it, dict):
            impact = str(it.get("impact") or "").strip()
            text = str(it.get("text") or it.get("title") or "").strip()
            s = f"{impact} {text}".strip()
        else:
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
    """
    “Опасные окна” (time_window) — вариант B:
    - aggressive: игнорирует окна полностью
    - neutral: в окнах по умолчанию ТОЛЬКО warning; no_trade только при stress
    - conservative: в окнах всегда no_trade
    """
    mode = normalize_mode(d.get("mode"))

    mins = _msk_minutes_from_time_str(d.get("time_msk"))
    if mins is None:
        mins = _msk_minutes_from_time_str(current_msk())
    if mins is None or not _in_danger_time_window_msk(mins):
        return

    d.setdefault("warnings", [])
    d.setdefault("no_trade_reasons", [])
    d.setdefault("no_trade_hint", "")

    if mode == "aggressive":
        reasons_before = d.get("no_trade_reasons") if isinstance(d.get("no_trade_reasons"), list) else []
        hint_before = d.get("no_trade_hint") if isinstance(d.get("no_trade_hint"), str) else ""
        had_tw = any(isinstance(r, str) and "time_window" in r for r in reasons_before) or ("time_window" in hint_before.lower())

        _tw_clear_artifacts(d)
        if had_tw and bool(d.get("no_trade")):
            reasons = d.get("no_trade_reasons") if isinstance(d.get("no_trade_reasons"), list) else []
            hint = d.get("no_trade_hint") if isinstance(d.get("no_trade_hint"), str) else ""
            if not reasons and not hint:
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

    # В окне, но без stress: time_window сам по себе не отключает neutral.
    if bool(d.get("no_trade")):
        reasons = d.get("no_trade_reasons")
        if isinstance(reasons, list):
            had_tw = any(isinstance(r, str) and "time_window" in r for r in reasons)
            d["no_trade_reasons"] = [
                r for r in reasons if not (isinstance(r, str) and "time_window" in r)
            ]
            if had_tw and not d["no_trade_reasons"]:
                d["no_trade"] = False
                d["no_trade_hint"] = ""


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


def _normalize_day_mid_context(d: dict) -> None:
    raw = d.get("day_mid_context")
    if isinstance(raw, dict):
        ctx = raw
    elif isinstance(raw, str):
        s = raw.strip()
        ctx = {"day_bias": None, "mid_bias": None, "notes": (s or None)}
    elif raw is None:
        ctx = {"day_bias": None, "mid_bias": None, "notes": None}
    else:
        try:
            notes = str(raw)
        except Exception:
            notes = None
        ctx = {"day_bias": None, "mid_bias": None, "notes": notes}

    if "day_bias" not in ctx:
        ctx["day_bias"] = None
    if "mid_bias" not in ctx:
        ctx["mid_bias"] = None
    if "notes" not in ctx:
        ctx["notes"] = None

    d["day_mid_context"] = ctx


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
    # Приоритет: Bybit USDT perpetual (swap/linear), чтобы совпадать по смыслу с EMA (Bybit Futures chart).
    try:
        ex = _get_exchange("bybit_swap")
        ex_sym = _normalize_bybit_swap_symbol(sym)
        last, change = _fetch_ticker(ex, ex_sym)
        if last is not None:
            return {"last": last, "change": change}
    except Exception:
        pass

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

def apply_live_price_hint(payload: dict) -> None:
    """
    SINGLE-mode helper: attach an exchange-derived price hint (source of truth for scale)
    into payload["hints"] before prompting the LLM.
    """
    if not isinstance(payload, dict):
        return
    hints = payload.get("hints")
    if not isinstance(hints, dict):
        return
    sym = hints.get("symbol")
    if not sym:
        return
    try:
        last = get_pair_ticker(sym).get("last")
    except Exception:
        last = None
    if last is None:
        return
    try:
        hints["price"] = _round_price(last)
        hints["price_source"] = "live"
    except Exception:
        return


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

        def _disable_aggressive(tag: str) -> None:
            agg = entries.get("aggressive")
            if not isinstance(agg, dict):
                return
            agg["enabled"] = False
            disabled_by = agg.get("disabled_by")
            if disabled_by is None:
                disabled_by_list: list[str] = []
            elif isinstance(disabled_by, list):
                disabled_by_list = [str(x) for x in disabled_by if str(x).strip()]
            elif isinstance(disabled_by, str) and disabled_by.strip():
                disabled_by_list = [disabled_by.strip()]
            else:
                disabled_by_list = []
            if tag and tag not in disabled_by_list:
                disabled_by_list.append(tag)
            if disabled_by_list:
                agg["disabled_by"] = disabled_by_list

        if state == "between":
            _disable_aggressive("ema_guard_between")
        if state == "below_both" and side == "long":
            _disable_aggressive("ema_guard_below_both_long")
        if state == "above_both" and side == "short":
            _disable_aggressive("ema_guard_above_both_short")

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

def _ema_relation_flag(price, ema) -> str:
    """
    Returns relation of price vs EMA in {"above","below","equal"}.
    If values are missing/unparseable, defaults to "equal" (neutral).
    """
    try:
        p = float(price)
        e = float(ema)
    except Exception:
        return "equal"
    if not (p == p and e == e):  # NaN guard
        return "equal"
    # Use a small relative tolerance for "equal" to avoid flip-flops due to rounding.
    tol = max(abs(p) * 1e-6, 1e-12)
    if abs(p - e) <= tol:
        return "equal"
    return "above" if p > e else "below"


def apply_ema_relation_flags(d: dict) -> None:
    """
    Adds explicit EMA relation flags used by the LLM and by rendering:
      - price_vs_ema20_m15: above|below|equal
      - price_vs_ema20_h1:  above|below|equal
      - ema_guard_state: above_both|below_both|between
      - ema_guard_notes: optional (derived from ema_guard.note/comment if present)
    Does NOT change trading logic.
    """
    if not isinstance(d, dict):
        return

    price = d.get("price")
    em15 = d.get("ema20_m15")
    em1h = d.get("ema20_h1")

    d["price_vs_ema20_m15"] = _ema_relation_flag(price, em15)
    d["price_vs_ema20_h1"] = _ema_relation_flag(price, em1h)

    # Derive guard state from relations when possible; otherwise keep neutral "between".
    m15 = d.get("price_vs_ema20_m15")
    h1 = d.get("price_vs_ema20_h1")
    if m15 == "above" and h1 == "above":
        state = "above_both"
    elif m15 == "below" and h1 == "below":
        state = "below_both"
    else:
        state = "between"
    d["ema_guard_state"] = state

    # Preserve any existing notes if already present.
    if "ema_guard_notes" not in d:
        eg = d.get("ema_guard")
        note = None
        if isinstance(eg, dict):
            note = eg.get("note") or eg.get("comment")
        if isinstance(note, str) and note.strip():
            d["ema_guard_notes"] = note.strip()


def enforce_ema_narrative_consistency(d: dict) -> None:
    """
    Minimal safety-net: ensure why_asset and multi_tf_view (m15/h1)
    do not contradict computed EMA relation flags.
    """
    if not isinstance(d, dict):
        return

    apply_ema_relation_flags(d)

    vs_m15 = (d.get("price_vs_ema20_m15") or "equal").strip().lower()
    vs_h1 = (d.get("price_vs_ema20_h1") or "equal").strip().lower()
    guard = (d.get("ema_guard_state") or "between").strip().lower()

    def _fix_line(text: str, desired: str) -> str:
        s = str(text or "")
        low = s.lower()
        wants_above = desired == "above"
        wants_below = desired == "below"
        wants_equal = desired == "equal"

        # If line makes an explicit contradictory EMA20 claim, rewrite to the desired polarity.
        if ("ema20" in low) and ("выше" in low or "ниже" in low or "над " in low or "под " in low):
            if wants_above and ("ниже" in low or "под " in low):
                return re.sub(r"(?i)\b(ниже|под)\b", "выше", s)
            if wants_below and ("выше" in low or "над " in low):
                return re.sub(r"(?i)\b(выше|над)\b", "ниже", s)
            if wants_equal:
                # Neutralize strong above/below wording while keeping the rest.
                s2 = re.sub(r"(?i)\b(выше|над|ниже|под)\b", "у", s)
                return s2
        return s

    # multi_tf_view: m15/h1 lines must be consistent if present.
    mtf = d.get("multi_tf_view")
    if isinstance(mtf, dict):
        if "m15" in mtf and isinstance(mtf.get("m15"), str):
            mtf["m15"] = _fix_line(mtf.get("m15", ""), vs_m15)
        if "h1" in mtf and isinstance(mtf.get("h1"), str):
            mtf["h1"] = _fix_line(mtf.get("h1", ""), vs_h1)
        d["multi_tf_view"] = mtf

    # why_asset: avoid generic "above/below EMA20" claims that contradict guard state.
    why = d.get("why_asset")
    if isinstance(why, str) and why.strip():
        low = why.lower()
        if "ema20" in low and ("выше" in low or "ниже" in low or "над " in low or "под " in low):
            if guard == "above_both":
                d["why_asset"] = re.sub(r"(?i)\b(ниже|под)\b", "выше", why)
            elif guard == "below_both":
                d["why_asset"] = re.sub(r"(?i)\b(выше|над)\b", "ниже", why)
            else:
                # between: keep neutral wording
                d["why_asset"] = re.sub(r"(?i)\b(выше|над|ниже|под)\b", "у", why)


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


def enforce_entry_price_order(d: dict) -> None:
    """
    Гарантирует геометрию entry_price_*:
    - SHORT: aggressive <= neutral <= conservative
    - LONG:  aggressive >= neutral >= conservative
    Корректирует ТОЛЬКО entry_price_* (side/direction/TP/SL/RR не трогает).
    """
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return

    def _as_float(x):
        try:
            v = float(x)
        except Exception:
            return None
        if not math.isfinite(v):
            return None
        return v

    agg = _as_float(d.get("entry_price_aggressive"))
    neu = _as_float(d.get("entry_price_neutral"))
    cons = _as_float(d.get("entry_price_conservative"))

    ref = neu
    if ref is None:
        ref = _as_float(_mid_from_range(d.get("entry_range")))
    if ref is None:
        entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
        ref = _as_float(_mid_from_range(((entries.get("neutral") or {}).get("range"))))
    if ref is None:
        ref = agg if agg is not None else cons

    if ref is None:
        return

    if agg is None:
        agg = ref
    if neu is None:
        neu = ref
    if cons is None:
        cons = ref

    if side == "short":
        if agg > neu:
            agg = neu
        if cons < neu:
            cons = neu
        if agg > cons:
            agg = cons
    else:  # long
        if agg < neu:
            agg = neu
        if cons > neu:
            cons = neu
        if agg < cons:
            agg = cons

    d["entry_price_aggressive"] = _round_price(agg)
    d["entry_price_neutral"] = _round_price(neu)
    d["entry_price_conservative"] = _round_price(cons)


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

    ema_m15 = d.get("ema_m15") if isinstance(d.get("ema_m15"), dict) else {}
    ema_h1 = d.get("ema_h1") if isinstance(d.get("ema_h1"), dict) else {}
    fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
    fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()

    def _ema_from(block: dict, key: str) -> float | None:
        v = _as_float(block.get(key))
        return v

    def _fan_aligned(state: str) -> bool:
        return (is_long and state == "bull") or ((not is_long) and state == "bear")

    fan_state = fan_m15 if fan_m15 in ("bull", "bear") else (fan_h1 if fan_h1 in ("bull", "bear") else "mixed")
    fan_is_mixed_or_against = (fan_state == "mixed") or (not _fan_aligned(fan_state))

    def _nearest_in_direction(values: list[float | None], *, above: float) -> float | None:
        cands = []
        for v in values:
            vv = _as_float(v)
            if vv is None:
                continue
            if is_long and vv > above:
                cands.append(vv)
            if (not is_long) and vv < above:
                cands.append(vv)
        if not cands:
            return None
        return min(cands) if is_long else max(cands)

    def _ensure_monotonic(entry: float, a: float, b: float, c: float | None) -> tuple[float, float, float | None]:
        min_step = abs(entry) * 0.001
        if is_long:
            if not (a > entry):
                a = entry + max(min_step, abs(entry) * 0.005)
            if not (b > a):
                b = a + max(min_step, abs(entry) * 0.005)
            if c is not None and not (c > b):
                c = b + max(min_step, abs(entry) * 0.005)
        else:
            if not (a < entry):
                a = entry - max(min_step, abs(entry) * 0.005)
            if not (b < a):
                b = a - max(min_step, abs(entry) * 0.005)
            if c is not None and not (c < b):
                c = b - max(min_step, abs(entry) * 0.005)
        return a, b, c

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
                fast_tvh = _nearest_in_direction(
                    [
                        _ema_from(ema_m15, "ema9"),
                        _ema_from(ema_m15, "ema12"),
                    ],
                    above=entry,
                )
                tvh1 = fast_tvh if fast_tvh is not None else entry * (1.005 if is_long else 0.995)
            if tvh2 is None or not _valid_tvh(entry, tvh2):
                thr = (
                    max(entry, float(tvh1) if _as_float(tvh1) is not None else entry)
                    if is_long
                    else min(entry, float(tvh1) if _as_float(tvh1) is not None else entry)
                )
                mid_tvh = _nearest_in_direction(
                    [
                        _ema_from(ema_m15, "ema20"),
                        _ema_from(ema_h1, "ema20"),
                    ],
                    above=thr,
                )
                tvh2 = mid_tvh if mid_tvh is not None else entry * (1.01 if is_long else 0.99)

            # VARIANT A: TP3 только если fan_state != mixed (и не против направления); иначе null.
            if fan_is_mixed_or_against:
                tvh3 = None
            else:
                if tvh3 is None or not _valid_tvh(entry, tvh3):
                    thr = (
                        max(entry, float(tvh2) if _as_float(tvh2) is not None else entry)
                        if is_long
                        else min(entry, float(tvh2) if _as_float(tvh2) is not None else entry)
                    )
                    far_tvh = _nearest_in_direction(
                        [
                            _ema_from(ema_h1, "ema20"),
                            _ema_from(ema_h1, "ema50"),
                            _ema_from(ema_h1, "ema200"),
                        ],
                        above=thr,
                    )
                    tvh3 = far_tvh if far_tvh is not None else entry * (1.02 if is_long else 0.98)

            tvh1 = float(_round_price(tvh1) if _round_price(tvh1) is not None else tvh1)
            tvh2 = float(_round_price(tvh2) if _round_price(tvh2) is not None else tvh2)
            tvh3_val = _as_float(tvh3)
            tvh3 = float(_round_price(tvh3_val) if (tvh3_val is not None and _round_price(tvh3_val) is not None) else tvh3_val) if tvh3_val is not None else None

            tvh1, tvh2, tvh3 = _ensure_monotonic(entry, tvh1, tvh2, tvh3)
            out_bucket = {"tvh1": tvh1, "tvh2": tvh2, "tvh3": tvh3}

        elif mode == "neutral":
            tvh1 = _pick_num("tvh1", "tp1")
            tvh2 = _pick_num("tvh2", "tp2")
            if tvh1 is None or not _valid_tvh(entry, tvh1):
                mid_tvh = _nearest_in_direction(
                    [
                        _ema_from(ema_m15, "ema20"),
                        _ema_from(ema_h1, "ema20"),
                    ],
                    above=entry,
                )
                tvh1 = mid_tvh if mid_tvh is not None else entry * (1.01 if is_long else 0.99)
            if tvh2 is None or not _valid_tvh(entry, tvh2):
                thr = (
                    max(entry, float(tvh1) if _as_float(tvh1) is not None else entry)
                    if is_long
                    else min(entry, float(tvh1) if _as_float(tvh1) is not None else entry)
                )
                h1_tvh = _nearest_in_direction(
                    [
                        _ema_from(ema_h1, "ema20"),
                        _ema_from(ema_h1, "ema50"),
                    ],
                    above=thr,
                )
                tvh2 = h1_tvh if h1_tvh is not None else entry * (1.02 if is_long else 0.98)
            tvh1 = float(_round_price(tvh1) if _round_price(tvh1) is not None else tvh1)
            tvh2 = float(_round_price(tvh2) if _round_price(tvh2) is not None else tvh2)
            tvh1, tvh2, _ = _ensure_monotonic(entry, tvh1, tvh2, None)
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
                near_tvh = _nearest_in_direction(
                    [
                        _ema_from(ema_m15, "ema20"),
                        _ema_from(ema_h1, "ema20"),
                    ],
                    above=entry,
                )
                tvh1 = near_tvh if near_tvh is not None else entry * (1.02 if is_long else 0.98)
            tvh1 = float(_round_price(tvh1) if _round_price(tvh1) is not None else tvh1)
            tvh1, _, _ = _ensure_monotonic(entry, tvh1, tvh1, None)

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

    # Если TP3 отключён (mixed/против направления) — не показываем TP3 в плане выхода, используем trail.
    try:
        agg_bucket = tp_by_mode.get("aggressive") if isinstance(tp_by_mode.get("aggressive"), dict) else {}
        if agg_bucket.get("tvh3") is None:
            txt = str(exit_plan_by_mode.get("aggressive") or "").strip()
            low = txt.lower()
            if txt and ("trail" not in low and "трейл" not in low):
                exit_plan_by_mode["aggressive"] = (txt + " Дальше — trail.").strip()
    except Exception:
        pass

    # Страховка структуры (в т.ч. при пропусках entry): ключи режимов всегда присутствуют.
    for m in VALID_MODES:
        rr_by_mode.setdefault(m, 0.0)

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


def validate_active_mode_setup(d: dict) -> dict:
    """
    Mode-specific validation (render contract):
    - проверяет, что для текущего режима есть entry/sl/tp/rr/exit_plan;
    - НЕ меняет торговую логику и НЕ пересчитывает уровни;
    - при невалидности помечает no_trade с понятным комментарием.
    """
    if bool(d.get("no_trade")):
        return d

    mode = normalize_mode(d.get("mode"))
    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}
    try:
        enabled = bucket.get("enabled", True)
    except Exception:
        enabled = True
    if enabled is False:
        fallback_mode = None
        for cand in ("neutral", "conservative"):
            if cand == mode:
                continue
            cb = entries.get(cand)
            if not isinstance(cb, dict):
                continue
            try:
                cand_enabled = cb.get("enabled", True)
            except Exception:
                cand_enabled = True
            if cand_enabled is True:
                fallback_mode = cand
                break

        if fallback_mode:
            warnings = d.setdefault("warnings", [])
            msg = f"mode_fallback: {mode}->{fallback_mode}"
            if msg not in warnings:
                warnings.append(msg)

            disabled_by = bucket.get("disabled_by") if isinstance(bucket, dict) else None
            if disabled_by:
                if isinstance(disabled_by, list):
                    tags = ",".join([str(x) for x in disabled_by if str(x).strip()])
                else:
                    tags = str(disabled_by).strip()
                if tags:
                    why = f"mode_disabled_by: {mode}: {tags}"
                    if why not in warnings:
                        warnings.append(why)

            d["mode"] = fallback_mode
            fb = entries.get(fallback_mode) if isinstance(entries.get(fallback_mode), dict) else {}
            fb_range = fb.get("range") if isinstance(fb.get("range"), dict) else None
            if fb_range is not None:
                d["entry_range"] = fb_range

            mode = fallback_mode
            bucket = fb
        else:
            d["no_trade"] = True
            d.setdefault("no_trade_reasons", []).append("mode_disabled")
            hint = f"Текущий режим ({mode}) отключён фильтром/валидатором — сигнал не выдан."
            disabled_by = bucket.get("disabled_by") if isinstance(bucket, dict) else None
            if disabled_by:
                if isinstance(disabled_by, list):
                    tags = ",".join([str(x) for x in disabled_by if str(x).strip()])
                else:
                    tags = str(disabled_by).strip()
                if tags:
                    hint = f"{hint} disabled_by={tags}"
            d["no_trade_hint"] = hint
            return d

    # Finalize mode + keep top-level entry_range consistent with it.
    # Important: final_mode must be computed AFTER any fallback logic above.
    final_mode = normalize_mode(d.get("mode"))
    d["mode"] = final_mode

    def _get_mode_range(m: str) -> dict | None:
        b = entries.get(m) if isinstance(entries.get(m), dict) else None
        r = b.get("range") if isinstance(b, dict) else None
        if not isinstance(r, dict):
            return None
        if "min" not in r and "max" not in r:
            return None
        return r

    final_range = _get_mode_range(final_mode)
    if final_range is None:
        final_range = _get_mode_range("neutral")
    if final_range is not None:
        d["entry_range"] = final_range

    mode = final_mode
    bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}

    missing: list[str] = []

    entry = _to_float(d.get(f"entry_price_{mode}"))
    if entry is None or not entry:
        missing.append("цена входа")

    sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
    sl_val = _to_float(sl_by_mode.get(mode))
    if sl_val is None or not sl_val:
        missing.append("SL")

    tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
    tp_bucket = tp_by_mode.get(mode) if isinstance(tp_by_mode.get(mode), dict) else {}

    def _tp_num(*keys: str) -> float | None:
        for k in keys:
            if k in tp_bucket:
                v = _to_float(tp_bucket.get(k))
                if v is not None and v:
                    return v
        return None

    if mode == "aggressive":
        if _tp_num("tvh1", "tp1") is None:
            missing.append("TP1")
        if _tp_num("tvh2", "tp2") is None:
            missing.append("TP2")
        # TP3 optional: может быть null (не валидируем как обязательный)
    elif mode == "neutral":
        if _tp_num("tvh1", "tp1") is None:
            missing.append("TP1")
        if _tp_num("tvh2", "tp2") is None:
            missing.append("TP2")
    else:  # conservative
        if _tp_num("tvh1", "tp1") is None:
            missing.append("TP1")
        tvh2_or_trail = tp_bucket.get("tvh2_or_trail")
        ok_trail = isinstance(tvh2_or_trail, str) and tvh2_or_trail.strip().lower() == "trail"
        ok_num = _to_float(tvh2_or_trail) is not None and bool(_to_float(tvh2_or_trail))
        ok_legacy_num = _tp_num("tp2") is not None
        if not (ok_trail or ok_num or ok_legacy_num):
            missing.append("TP2_or_trail")

    rr_by_mode = d.get("rr_by_mode") if isinstance(d.get("rr_by_mode"), dict) else {}
    rr_val = _to_float(rr_by_mode.get(mode))
    if rr_val is None or rr_val <= 0:
        missing.append("RR")

    ep_by_mode = d.get("exit_plan_by_mode") if isinstance(d.get("exit_plan_by_mode"), dict) else {}
    ep_txt = ep_by_mode.get(mode)
    if not isinstance(ep_txt, str) or not ep_txt.strip():
        missing.append("план выхода")

    if missing:
        d["no_trade"] = True
        d.setdefault("no_trade_reasons", []).append("invalid_mode_setup")
        miss = ", ".join(missing)
        d["no_trade_hint"] = f"Невалидные данные для текущего режима ({mode}): отсутствует/некорректно: {miss}."
    return d


def finalize_signal(data: dict, hints: dict | None = None, *, fetch_price: bool = True) -> dict:
    hints = hints or {}
    d = data or {}

    # Защита от cross-symbol contamination в hints.*:
    # hints.price / hints.ema* можно использовать только если hints.symbol == текущему d["symbol"].
    hints_symbol = hints.get("symbol")
    if not d.get("symbol") and hints_symbol:
        d["symbol"] = hints_symbol
    if hints.get("time_msk"):
        d["time_msk"] = hints["time_msk"]
    symbol = d.get("symbol")
    DRY_RUN = os.getenv("DRY_RUN") == "1"
    hints_match_symbol = bool(hints_symbol) and bool(symbol) and hints_symbol == symbol
    if hints_match_symbol and "price" in hints and hints["price"] is not None:
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

    if fetch_price and symbol and not DRY_RUN and not d.get("price"):
        try:
            ticker = get_pair_ticker(symbol)
            rounded = _round_price(ticker.get("last"))
            if rounded is not None:
                d["price"] = rounded
        except Exception:
            pass

    sym_for_ema = symbol
    try:
        ema20_m15 = _to_float(hints.get("ema20_m15")) if hints_match_symbol else None
        if ema20_m15 is None:
            ema20_m15 = _to_float(d.get("ema20_m15"))
        if ema20_m15 is None and sym_for_ema:
            ema20_m15 = get_ema20_m15(sym_for_ema)
        d["ema20_m15"] = ema20_m15

        ema20_h1 = _to_float(hints.get("ema20_h1")) if hints_match_symbol else None
        if ema20_h1 is None:
            ema20_h1 = _to_float(d.get("ema20_h1"))
        if ema20_h1 is None and sym_for_ema:
            ema20_h1 = get_ema20_h1(sym_for_ema)
        d["ema20_h1"] = ema20_h1
    except Exception:
        pass

    def _refresh_price() -> float | None:
        if not symbol:
            return None
        try:
            return _round_price(get_pair_ticker(symbol).get("last"))
        except Exception:
            return None

    # Price refresh policy:
    # A) DRY_RUN: price всегда берём с биржи (source of truth).
    # B) Non-DRY_RUN: если price явно "битый" относительно EMA20(M15) — один раз рефрешим.
    try:
        if DRY_RUN and symbol:
            refreshed_price = _refresh_price()
            if refreshed_price is not None:
                d["price"] = refreshed_price
        elif symbol:
            price_val = _to_float(d.get("price"))
            ema20_m15_val = _to_float(d.get("ema20_m15"))
            if price_val is not None and price_val > 0 and ema20_m15_val is not None:
                ratio = abs(price_val - ema20_m15_val) / price_val
                if ratio > 0.15:
                    refreshed_price = _refresh_price()
                    if refreshed_price is not None:
                        d["price"] = refreshed_price
    except Exception:
        pass

    # Минимальная самопроверка источника EMA (только warnings, без блокировок)
    try:
        warnings = d.setdefault("warnings", [])
        ema20_m15 = _to_float(d.get("ema20_m15"))
        ema20_h1 = _to_float(d.get("ema20_h1"))
        price_val = _to_float(d.get("price"))

        if ema20_m15 is None and ema20_h1 is None:
            if "ema_source_missing" not in warnings:
                warnings.append("ema_source_missing")
        elif (
            price_val
            and ema20_m15 is not None
            and ema20_h1 is not None
            and abs(ema20_m15 - ema20_h1) / price_val > 0.05
        ):
            if "ema_source_suspect" not in warnings:
                warnings.append("ema_source_suspect")
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

    # Explicit EMA relation flags (payload + rendering) + narrative safety net.
    try:
        apply_ema_relation_flags(d)
        enforce_ema_narrative_consistency(d)
    except Exception:
        pass

    apply_direction_guard(d)
    _normalize_day_mid_context(d)
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
    _debug_trace_set_entry_range("entry_range_post_entries", d)

    try:
        apply_ema_blocks_and_derivatives(d, sym_for_ema)
        apply_entry_prices_from_ranges(d)
        enforce_entry_price_order(d)
    except Exception:
        pass

    apply_ema_exhale_filter(d)

    validate_or_fallback_tvh_by_mode(d)
    _debug_trace_set_entry_range("entry_range_post_tvh", d)
    validate_active_mode_setup(d)
    apply_time_window_policy_variant_b(d)
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
_CLI_CODE = r'''
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
        "Используй уже существующие поля:\n"
        "- ema_m15, ema_h1 (уже рассчитанные EMA)\n"
        "- ema_fan_m15_state, ema_fan_h1_state (bull/bear/mixed)\n"
        "- pivot_ema_hint_by_mode (какой EMA использовать как якорь по режиму)\n"
        "\nТРЕБОВАНИЕ (VARIANT A, по режимам) для tp_by_mode:\n"
        "- aggressive: {tvh1, tvh2, tvh3}\n"
        "  - tvh1: ближе (структура M15, ориентир на fast EMA9/EMA12)\n"
        "  - tvh2: средняя цель (контекст EMA20 на M15–H1)\n"
        "  - tvh3: дальняя цель ТОЛЬКО если fan_state != mixed; иначе tvh3 = null и вместо TP3 используй trail\n"
        "- neutral: {tvh1, tvh2}\n"
        "  - tvh1: около средней цели (EMA20)\n"
        "  - tvh2: дальше (H1 контекст)\n"
        "- conservative: {tvh1, tvh2_or_trail}\n"
        "  - tvh1: ближе\n"
        "  - tvh2_or_trail: всегда строка \"trail\" (дальше только трейлинг)\n"
        "Все tvh* должны быть ЧИСЛАМИ (не формулы вида \"entry+2%\"), строго в сторону direction.\n"
        "\nТРЕБОВАНИЕ (VARIANT A) для exit_plan_by_mode:\n"
        "- сделай реально разным для режимов; кратко (1–2 короткие строки на режим, можно одной строкой)\n"
        "- обязательно словами (БЕЗ чисел EMA и БЕЗ процентов) упомяни логику:\n"
        "  fan расширяется → цели шире / trail позже; fan схлопывается или mixed → цели ближе / trail раньше\n"
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

    if _DEBUG_TRACE_ENABLED:
        _debug_trace_reset()
        _debug_trace_update_main_fields(signal_raw)
        _debug_trace_set_entry_range("entry_range_raw", signal_raw)
        _DEBUG_TRACE["entry_range_source"] = (  # type: ignore[index]
            "llm_raw" if "entry_range" in signal_raw else "missing"
        )
        _debug_trace_set_entry_range("entry_range_pre_finalize", signal_raw)

    hints = {"time_msk": time_msk, "mode": mode}
    signal = finalize_signal(signal_raw, hints)
    signal["time_msk"] = time_msk

    _debug_trace_set_entry_range("entry_range_post_finalize", signal)
    _debug_trace_set_entry_range("entry_range_final", signal)
    _debug_trace_write()

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
    # live price (source of truth for price scale)
    apply_live_price_hint(payload)

# time hint
payload["hints"]["time_msk"] = current_msk()

# EMA20 hints
_sym = payload["hints"].get("symbol")
payload["hints"]["ema20_m15"] = get_ema20_m15(_sym) if _sym else None
payload["hints"]["ema20_h1"] = get_ema20_h1(_sym) if _sym else None
try:
    apply_ema_relation_flags(payload.get("hints", {}))
except Exception:
    pass

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
    "- ema_m15, ema_h1 (уже рассчитанные EMA)\n"
    "- ema_fan_m15_state, ema_fan_h1_state (bull/bear/mixed)\n"
    "- pivot_ema_hint_by_mode\n"
    "\nТРЕБОВАНИЕ (VARIANT A, по режимам) для tp_by_mode:\n"
    "- aggressive: {tvh1, tvh2, tvh3}\n"
    "  - tvh1: ближе (структура M15, ориентир на fast EMA9/EMA12)\n"
    "  - tvh2: средняя цель (контекст EMA20 на M15–H1)\n"
    "  - tvh3: дальняя цель ТОЛЬКО если fan_state != mixed; иначе tvh3 = null и вместо TP3 используй trail\n"
    "- neutral: {tvh1, tvh2}\n"
    "  - tvh1: около средней цели (EMA20)\n"
    "  - tvh2: дальше (H1 контекст)\n"
    "- conservative: {tvh1, tvh2_or_trail}\n"
    "  - tvh1: ближе\n"
    "  - tvh2_or_trail: всегда строка \"trail\" (дальше только трейлинг)\n"
    "Все tvh* должны быть ЧИСЛАМИ (не формулы вида \"entry+2%\"), строго в сторону direction.\n"
    "\nТРЕБОВАНИЕ (VARIANT A) для exit_plan_by_mode:\n"
    "- сделай реально разным для режимов; кратко (1–2 короткие строки на режим, можно одной строкой)\n"
    "- обязательно словами (БЕЗ чисел EMA и БЕЗ процентов) упомяни логику:\n"
    "  fan расширяется → цели шире / trail позже; fan схлопывается или mixed → цели ближе / trail раньше\n"
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

if _DEBUG_TRACE_ENABLED:
    _debug_trace_reset()
    _debug_trace_update_main_fields(data if isinstance(data, dict) else None)
    if isinstance(data, dict):
        _debug_trace_set_entry_range("entry_range_raw", data)
        _DEBUG_TRACE["entry_range_source"] = (  # type: ignore[index]
            "llm_raw" if "entry_range" in data else "missing"
        )
        _debug_trace_set_entry_range("entry_range_pre_finalize", data)

data = finalize_signal(data, payload.get("hints", {}))

_debug_trace_set_entry_range("entry_range_post_finalize", data)
_debug_trace_set_entry_range("entry_range_final", data)
_debug_trace_write()

print(json.dumps(data, ensure_ascii=False))
sys.exit(0)
'''


if __name__ == "__main__":
    if OpenAI is None:
        print("ERROR: openai package not installed", file=sys.stderr)
        sys.exit(1)
    if ccxt is None:
        print("ERROR: ccxt package not installed", file=sys.stderr)
        sys.exit(1)
    exec(_CLI_CODE, globals(), globals())
