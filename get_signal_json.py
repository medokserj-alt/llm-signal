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

# ---- Variant B+2: neutral semantics guard ----
# Neutral mode must not recommend entries "too close" to current price.
# Thresholds are configurable here (single place).
NEUTRAL_MIN_DIST_PCT_MAJOR = 0.25  # BTC/, ETH/
NEUTRAL_MIN_DIST_PCT_ALT = 0.35  # others

# ---- Neutral volatility-aware spacing (adaptive calmer entry) ----
# Applies only to entry_price_neutral placement; does not change EMA/provenance, direction, SL/TP/RR math, or gates.
NEUTRAL_VOL_MIN_OFFSET_PCT_MAJOR = 0.005  # 0.50%
NEUTRAL_VOL_MIN_OFFSET_PCT_ALT = 0.007  # 0.70%
NEUTRAL_VOL_K_ATR_MAJOR = 1.0
NEUTRAL_VOL_K_ATR_ALT = 1.2

# When neutral mode exposes an early aggressive entry option, keep neutral meaningfully farther.
# Soft shaping only: does not hard-limit the model or change EMA/no_trade/direction logic.
NEUTRAL_BUFFER_TICKS = 10

# Near-market definition (neutral): if neutral entry is within this many ticks from current price,
# and no aggressive_option exists, split into (aggressive_option=original) + buffered neutral entry.
NEUTRAL_NEAR_TICKS = 10

# ---- Flush gate (neutral must not knife-catch) ----
# "Flush" is defined relative to ATR(14) on M15 candles.
# X: two consecutive candles body >= X * ATR(14)
# Y: one candle body >= Y * ATR(14)
#
# Thresholds are intentionally configurable via env for fast tuning without code changes.
try:
    FLUSH_BODY_X_ATR = float(os.getenv("FLUSH_BODY_X_ATR") or "1.1")
except Exception:  # pragma: no cover
    FLUSH_BODY_X_ATR = 1.1
try:
    FLUSH_BODY_Y_ATR = float(os.getenv("FLUSH_BODY_Y_ATR") or "1.8")
except Exception:  # pragma: no cover
    FLUSH_BODY_Y_ATR = 1.8

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
        # Keep only a short tail of OHLCV for derived calculations (ATR/flush), not for full history.
        # Format is ccxt OHLCV: [ts, open, high, low, close, volume?]
        "ohlcv_tail": ohlcv[-60:] if isinstance(ohlcv, list) else None,
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

    # Источник: Bybit swap (USDT perpetual / linear) — единый источник истины.
    for market in ("bybit_swap",):
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
        "ohlcv_tail": None,
        "closes_tail": None,
        "symbol": (symbol or "").strip() or None,
        "market": None,
    }

    if p <= 1:
        return out

    warmup_len = max(500, p * 20)
    # Single source of truth: Bybit USDT perpetual (linear swap).
    for market in ("bybit_swap",):
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
        out["ohlcv_tail"] = snap.get("ohlcv_tail")
        try:
            out["closes_tail"] = [float(x) for x in closes[-5:]]
        except Exception:
            out["closes_tail"] = None

        # Keep provenance identity stable.
        out["exchange"] = "bybit"
        out["market_type"] = "linear_perp"

        return out

    return out


def overwrite_ema20_from_provenance(d: dict) -> None:
    """
    Enforce a single source of truth for EMA20(M15/H1):
    always overwrite EMA values from computed OHLCV provenance (Bybit linear perp),
    never from LLM text/hints.
    """
    if not isinstance(d, dict):
        return
    sym = (d.get("symbol") or "").strip()
    if not sym:
        return

    # Keep identity fields stable (EMA provenance source, not a price hint).
    d["exchange"] = "bybit"
    d["market_type"] = "linear_perp"
    d["price_source"] = "last"

    prov_m15 = get_ema_provenance(20, "15m", symbol=sym)
    d["ema20_m15"] = prov_m15.get("ema")
    d["timeframe_m15"] = prov_m15.get("timeframe") or "15m"
    d["candles_m15_count"] = prov_m15.get("candles_count")
    d["last_candle_m15"] = prov_m15.get("last_candle")
    d["ohlcv_m15_tail"] = prov_m15.get("ohlcv_tail")
    d["closes_m15_tail"] = prov_m15.get("closes_tail")

    prov_h1 = get_ema_provenance(20, "1h", symbol=sym)
    d["ema20_h1"] = prov_h1.get("ema")
    d["timeframe_h1"] = prov_h1.get("timeframe") or "1h"
    d["candles_h1_count"] = prov_h1.get("candles_count")
    d["last_candle_h1"] = prov_h1.get("last_candle")
    d["ohlcv_h1_tail"] = prov_h1.get("ohlcv_tail")
    d["closes_h1_tail"] = prov_h1.get("closes_tail")


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


def _atr14_from_ohlcv_tail(ohlcv_tail) -> float | None:
    """
    Simple ATR(14) from a short OHLCV tail (ccxt format):
      [ts, open, high, low, close, volume?]
    Uses SMA of the last 14 true ranges (requires >= 15 candles).
    """
    if not isinstance(ohlcv_tail, list) or len(ohlcv_tail) < 15:
        return None

    candles: list[tuple[float, float, float, float]] = []
    for row in ohlcv_tail:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
        except Exception:
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)):
            continue
        candles.append((o, h, l, c))

    if len(candles) < 15:
        return None

    candles = candles[-15:]
    trs: list[float] = []
    prev_close = candles[0][3]
    for (_o, h, l, c) in candles[1:]:
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        if math.isfinite(tr) and tr >= 0:
            trs.append(float(tr))
        prev_close = c

    if len(trs) != 14:
        return None
    atr = sum(trs) / 14.0
    if not (math.isfinite(atr) and atr > 0):
        return None
    return float(atr)


def _symbol_is_major(symbol: str | None) -> bool:
    sym = (symbol or "").strip().upper()
    return sym.startswith("BTC/") or sym.startswith("ETH/") or sym.startswith("BTCUSDT") or sym.startswith("ETHUSDT")


def _m15_volatility_metrics(d: dict) -> tuple[float | None, float | None]:
    """
    Returns (atr14_m15, range_pct_m15) where range_pct_m15 is computed from the last 20 candles:
      (max_high - min_low) / price
    """
    if not isinstance(d, dict):
        return (None, None)
    ohlcv_tail = d.get("ohlcv_m15_tail")
    if not isinstance(ohlcv_tail, list) or len(ohlcv_tail) < 20:
        return (None, None)

    atr14 = _atr14_from_ohlcv_tail(ohlcv_tail)

    px = _to_float(d.get("price"))
    if px is None or not (math.isfinite(px) and px > 0):
        return (atr14, None)

    highs: list[float] = []
    lows: list[float] = []
    for row in ohlcv_tail[-20:]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        h = _to_float(row[2])
        l = _to_float(row[3])
        if h is None or l is None:
            continue
        highs.append(float(h))
        lows.append(float(l))
    if not highs or not lows:
        return (atr14, None)
    rng = max(highs) - min(lows)
    if not (math.isfinite(rng) and rng > 0):
        return (atr14, None)
    return (atr14, float(rng) / float(px))


def ensure_aggressive_option(d: dict, *, entries: dict | None, note: str) -> None:
    """
    Ensures d["aggressive_option"] exists with a single concrete entry price.
    Uses only already computed fields / existing envelopes; does not change aggressive entry placement logic.
    """
    if not isinstance(d, dict):
        return

    existing = d.get("aggressive_option")
    if isinstance(existing, dict) and _to_float(existing.get("entry_price")) is not None:
        if isinstance(note, str) and note.strip():
            existing["note"] = note.strip()
        d["aggressive_option"] = existing
        return

    entry = _to_float(d.get("entry_price_aggressive"))
    if entry is None:
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if isinstance(entries, dict):
            ab = entries.get("aggressive") if isinstance(entries.get("aggressive"), dict) else None
            ar = (ab or {}).get("range") if isinstance(ab, dict) else None
            if isinstance(ar, dict):
                a_min = _to_float(ar.get("min"))
                a_max = _to_float(ar.get("max"))
                if a_min is not None and a_max is not None:
                    lo, hi = (min(a_min, a_max), max(a_min, a_max))
                    if side == "long":
                        entry = hi
                    elif side == "short":
                        entry = lo
                    else:
                        entry = (lo + hi) / 2.0
    if entry is None:
        entry = _to_float(d.get("entry_price_neutral"))
    if entry is None:
        er = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None
        if isinstance(er, dict):
            e_min = _to_float(er.get("min"))
            e_max = _to_float(er.get("max"))
            if e_min is not None and e_max is not None:
                entry = (min(e_min, e_max) + max(e_min, e_max)) / 2.0

    if entry is None:
        return

    out_note = note.strip() if isinstance(note, str) and note.strip() else ""
    d["aggressive_option"] = {"entry_price": float(entry)}
    if out_note:
        d["aggressive_option"]["note"] = out_note


def _m15_flush_detected(d: dict) -> bool:
    """
    Directional flush detector for the planned side:
    - LONG: bearish flush (dump)
    - SHORT: bullish flush (pump)
    """
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return False

    is_long = side == "long"
    fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
    vs_m15 = str(d.get("price_vs_ema20_m15") or "").strip().lower()

    # Structural fallback (available even without OHLCV tail).
    if is_long and (fan_m15 == "bear" and vs_m15 == "below"):
        return True
    if (not is_long) and (fan_m15 == "bull" and vs_m15 == "above"):
        return True

    ohlcv_tail = d.get("ohlcv_m15_tail")
    atr14 = _atr14_from_ohlcv_tail(ohlcv_tail)
    if atr14 is None:
        return False
    if not isinstance(ohlcv_tail, list) or len(ohlcv_tail) < 2:
        return False

    parsed: list[tuple[float, float]] = []
    for row in ohlcv_tail[-2:]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            o = float(row[1])
            c = float(row[4])
        except Exception:
            continue
        if not all(math.isfinite(v) for v in (o, c)):
            continue
        parsed.append((o, c))

    if len(parsed) < 2:
        return False

    def body_in_flush_direction(o: float, c: float) -> float:
        if is_long:
            return abs(c - o) if c < o else 0.0
        return abs(c - o) if c > o else 0.0

    last_body = body_in_flush_direction(parsed[-1][0], parsed[-1][1])
    prev_body = body_in_flush_direction(parsed[-2][0], parsed[-2][1])

    x = float(FLUSH_BODY_X_ATR)
    y = float(FLUSH_BODY_Y_ATR)
    if y > 0 and last_body >= y * atr14:
        return True
    if x > 0 and (last_body >= x * atr14 and prev_body >= x * atr14):
        return True

    return False


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
    """
    “Green lane” inside time_window for neutral:
    allow only exceptionally good continuation + stable conditions.
    Uses existing facts only (no EMA/RR/SL/TP changes).
    """
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
    """
    Time-window policy (updated):
    - aggressive: block ONLY on extreme conditions; otherwise allow but force wait_confirm (+ caution warning)
    - neutral: block by default, but allow a “green lane” for exceptionally good continuation+stable setups
    - conservative: always no_trade inside time_window
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
        _tw_append_unique(d, "warnings", "time_window_low_liquidity")
        _tw_append_unique(d, "warnings", "time_window_caution_aggressive")

        # Never “enter now” inside time_window in aggressive mode.
        d["entry_mode"] = "wait_confirm"

        warnings = d.get("warnings")
        stress_news = _tw_news_stress(d.get("news_context"))
        stress_vol = _tw_volatility_stress(warnings)
        stress_struct = _tw_structure_stress(d)
        stress_risk_off = _tw_risk_off_stress(d)
        stress = bool(stress_news or stress_vol or stress_struct or stress_risk_off)

        flush_extreme = _tw_has_any_reason(d, "flush_knife_aggressive_extreme") or bool(d.get("flush_knife_aggressive_extreme"))
        invalid_setup = _tw_has_any_reason(d, "invalid_mode_setup")

        extreme_block = bool(stress or flush_extreme or invalid_setup)
        if extreme_block:
            d["no_trade"] = True
            _tw_append_unique(d, "no_trade_reasons", "time_window_extreme_block")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "time_window_extreme_block"
        return

    warnings = d.get("warnings")
    _tw_append_unique(d, "warnings", "time_window_low_liquidity")

    if mode == "conservative":
        d["no_trade"] = True
        reasons = d.get("no_trade_reasons")
        if isinstance(reasons, list) and "time_window" not in reasons:
            reasons.append("time_window")
        if isinstance(reasons, list) and "time_window_conservative" not in reasons:
            reasons.append("time_window_conservative")
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
        hints["price"] = _round_price(last, symbol=sym)
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
            # Special-case: if there is a confirmed micro phase-flip on M15 for the proposed direction,
            # aggressive should adapt tactics (wait_confirm) rather than be blocked.
            try:
                if _compute_phase_flip_m15(d):
                    d["entry_mode"] = "wait_confirm"
                    if isinstance(warnings, list) and "phase_flip_wait_confirm" not in warnings:
                        warnings.append("phase_flip_wait_confirm")
                    return
            except Exception:
                pass

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


US_SESSION_UTC_WINDOW_DEFAULT = "14:00-21:00"


def _parse_utc_window_minutes(spec: str) -> tuple[int, int] | None:
    """
    Parses "HH:MM-HH:MM" into (start_min, end_min) minutes from midnight UTC.
    End is treated as exclusive. Supports windows that may cross midnight.
    """
    try:
        s = str(spec or "").strip()
    except Exception:
        return None
    if not s:
        return None
    m = re.findall(r"(\d{1,2}):(\d{2})", s)
    if len(m) < 2:
        return None
    (h1s, m1s), (h2s, m2s) = m[0], m[1]
    try:
        h1, m1 = int(h1s), int(m1s)
        h2, m2 = int(h2s), int(m2s)
    except Exception:
        return None
    if not (0 <= h1 <= 23 and 0 <= m1 <= 59 and 0 <= h2 <= 23 and 0 <= m2 <= 59):
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def _utc_minutes_from_signal_time(d: dict) -> int | None:
    """
    Best-effort UTC minutes:
    - prefer last_candle_m15.ts (exchange candle timestamp),
    - fallback to parsing time_msk (Europe/Moscow) and converting to UTC.
    """
    try:
        lc = d.get("last_candle_m15")
        if isinstance(lc, dict) and lc.get("ts") is not None:
            ts_ms = int(lc.get("ts"))
            dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=ZoneInfo("UTC"))
            return int(dt.hour) * 60 + int(dt.minute)
    except Exception:
        pass

    try:
        s = str(d.get("time_msk") or "").strip()
    except Exception:
        s = ""
    if not s:
        return None

    try:
        m = re.search(r"(?:(\d{1,2})\.(\d{1,2})\.(\d{4})\s*,\s*)?(\d{1,2}):(\d{2})", s)
        if not m:
            return None
        dd = int(m.group(1) or datetime.now(ZoneInfo("Europe/Moscow")).day)
        mm = int(m.group(2) or datetime.now(ZoneInfo("Europe/Moscow")).month)
        yy = int(m.group(3) or datetime.now(ZoneInfo("Europe/Moscow")).year)
        hh = int(m.group(4))
        mi = int(m.group(5))
        dt_msk = datetime(yy, mm, dd, hh, mi, tzinfo=ZoneInfo("Europe/Moscow"))
        dt_utc = dt_msk.astimezone(ZoneInfo("UTC"))
        return int(dt_utc.hour) * 60 + int(dt_utc.minute)
    except Exception:
        return None


def _is_in_utc_window(mins_utc: int, start_min: int, end_min: int) -> bool:
    if not (0 <= mins_utc < 24 * 60):
        return False
    start_min = int(start_min) % (24 * 60)
    end_min = int(end_min) % (24 * 60)
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= mins_utc < end_min
    return mins_utc >= start_min or mins_utc < end_min


def _is_us_session(d: dict) -> bool:
    """
    Simple configurable US session flag.
    Default window is 14:00–21:00 UTC, configurable via env US_SESSION_UTC_WINDOW="HH:MM-HH:MM".
    """
    try:
        spec = os.getenv("US_SESSION_UTC_WINDOW", US_SESSION_UTC_WINDOW_DEFAULT)
    except Exception:
        spec = US_SESSION_UTC_WINDOW_DEFAULT
    window = _parse_utc_window_minutes(spec) or _parse_utc_window_minutes(US_SESSION_UTC_WINDOW_DEFAULT)
    if not window:
        return False
    mins_utc = _utc_minutes_from_signal_time(d)
    if mins_utc is None:
        return False
    return _is_in_utc_window(mins_utc, window[0], window[1])


def _compute_phase_flip_m15(d: dict) -> bool:
    """
    Phase-flip (micro-phase change) on M15 for the proposed trade direction:
    - SHORT ideas: true if price_vs_ema20_m15 == "above" and last 2 M15 closes are above ema20_m15
    - LONG ideas:  true if price_vs_ema20_m15 == "below" and last 2 M15 closes are below ema20_m15
    """
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return False

    vs_m15 = str(d.get("price_vs_ema20_m15") or "").strip().lower()
    ema20_m15 = _to_float(d.get("ema20_m15"))
    closes = d.get("closes_m15_tail")
    if ema20_m15 is None or not isinstance(closes, list) or len(closes) < 2:
        return False

    last2: list[float] = []
    for x in closes[-2:]:
        v = _to_float(x)
        if v is None:
            return False
        last2.append(v)

    if side == "short":
        if vs_m15 != "above":
            return False
        return bool(last2[0] > ema20_m15 and last2[1] > ema20_m15)
    # side == "long"
    if vs_m15 != "below":
        return False
    return bool(last2[0] < ema20_m15 and last2[1] < ema20_m15)


def _compute_impulse_proxy(d: dict) -> bool:
    """
    Impulse proxy: reuse existing computed signals (warnings + flush proxies).
    """
    warnings = d.get("warnings")
    wl = ""
    if isinstance(warnings, list):
        wl = " ".join(str(w or "").strip().lower() for w in warnings)
    if any(k in wl for k in ("impulse_no_exhale", "phase_between", "ema_between_m15_h1")):
        return True
    if _m15_flush_detected(d):
        return True
    if bool(d.get("flush_knife_aggressive_extreme")):
        return True
    reasons = d.get("no_trade_reasons")
    if isinstance(reasons, list):
        rl = " ".join(str(r or "").strip().lower() for r in reasons)
        if "flush" in rl or "knife" in rl or "impulse" in rl:
            return True
    return False


def _append_unique_str(d: dict, key: str, value: str) -> None:
    if not value:
        return
    xs = d.get(key)
    if not isinstance(xs, list):
        return
    if value not in xs:
        xs.append(value)


def _set_no_trade_primary_reason(d: dict, reason: str) -> None:
    d["no_trade"] = True
    reasons = d.get("no_trade_reasons")
    if not isinstance(reasons, list):
        reasons = []
    # Waiting-confirmation is a generic placeholder; phase-flip reasons should be primary when present.
    reasons = [r for r in reasons if str(r or "").strip() and str(r).strip() != "waiting_confirmation"]
    reasons = [reason] + [r for r in reasons if r != reason]
    d["no_trade_reasons"] = reasons
    d["no_trade_hint"] = reason


def _ensure_aggressive_option_note(d: dict, note: str, *, force: bool = False) -> None:
    if not note:
        return
    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    agg = entries.get("aggressive") if isinstance(entries.get("aggressive"), dict) else {}
    enabled = agg.get("enabled", True) is not False
    if not enabled and not force:
        return

    entry = _to_float(d.get("entry_price_aggressive"))
    if entry is None:
        # Fall back to existing entry_range anchor (no new math).
        er = agg.get("range") if isinstance(agg.get("range"), dict) else None
        if isinstance(er, dict):
            e_min = _to_float(er.get("min"))
            e_max = _to_float(er.get("max"))
            if e_min is not None and e_max is not None:
                entry = (min(e_min, e_max) + max(e_min, e_max)) / 2.0
    if entry is None:
        return

    d["aggressive_option"] = {"entry_price": float(entry), "note": note}


def apply_phase_flip_modifier(d: dict) -> None:
    """
    Phase-flip modifier (micro-phase change) based on EMA20(M15) behavior.
    Uses only existing computed data (EMA flags, closes_m15_tail, warnings, flush proxies).
    Does NOT modify EMA/ATR provenance or SL/TP/RR math.
    """
    if not isinstance(d, dict):
        return

    phase_flip_m15 = _compute_phase_flip_m15(d)
    impulse_proxy = _compute_impulse_proxy(d)
    is_us_session = _is_us_session(d)

    # Persist to top-level JSON (required for logs/last.json consumers).
    # Always booleans; if inputs are missing, compute_* helpers return False.
    d["phase_flip_m15"] = bool(phase_flip_m15)
    d["impulse_proxy"] = bool(impulse_proxy)
    d["is_us_session"] = bool(is_us_session)

    dbg = d.setdefault("debug", {})
    if isinstance(dbg, dict):
        dbg["phase_flip_m15"] = bool(phase_flip_m15)
        dbg["impulse_proxy"] = bool(impulse_proxy)
        dbg["is_us_session"] = bool(is_us_session)

    if not (phase_flip_m15 and impulse_proxy):
        return

    mode = normalize_mode(d.get("mode"))

    # NEUTRAL: hard stop during US session; soft wait outside.
    if mode == "neutral":
        if is_us_session:
            _set_no_trade_primary_reason(d, "us_session_phase_flip_neutral_pause")
            _ensure_aggressive_option_note(
                d,
                "US-сессия: идёт перераспределение/выдох после импульса — neutral пауза; "
                "trade допустим только в aggressive при подтверждении.",
                force=True,
            )
        else:
            _set_no_trade_primary_reason(d, "phase_flip_neutral_wait")
            _ensure_aggressive_option_note(
                d,
                "Идёт перераспределение/выдох после импульса — neutral ждёт подтверждение; "
                "trade возможен только при подтверждении (или в aggressive с осторожностью).",
                force=True,
            )

        # Do NOT emit neutral continuation levels when paused.
        try:
            d.pop("entry_price_neutral", None)
            d.pop("sl", None)
            d.pop("tp1", None)
            d.pop("tp2", None)
            d.pop("tp3", None)
        except Exception:
            pass
        return

    # AGGRESSIVE: do not block, adapt tactics.
    if mode == "aggressive" and not bool(d.get("no_trade")):
        # Allow aggressive to stay tradable: do not auto-fallback solely due to ema_guard disables
        # when a confirmed phase flip is present (tactical adaptation).
        entries = d.get("entries") if isinstance(d.get("entries"), dict) else None
        if isinstance(entries, dict):
            agg = entries.get("aggressive") if isinstance(entries.get("aggressive"), dict) else None
            if isinstance(agg, dict) and agg.get("enabled") is False:
                disabled_by = agg.get("disabled_by")
                tags: list[str] = []
                if isinstance(disabled_by, str) and disabled_by.strip():
                    tags = [disabled_by.strip()]
                elif isinstance(disabled_by, list):
                    tags = [str(x).strip() for x in disabled_by if str(x).strip()]
                allowed = {"ema_guard_between", "ema_guard_above_both_short", "ema_guard_below_both_long"}
                if tags and all(t in allowed for t in tags):
                    agg["enabled"] = True
                    entries["aggressive"] = agg
                    d["entries"] = entries

        d["entry_mode"] = "wait_confirm"
        d.setdefault("warnings", [])
        _append_unique_str(d, "warnings", "phase_flip_wait_confirm")
        if is_us_session:
            _append_unique_str(
                d,
                "warnings",
                "US-сессия: перераспределение после импульса — возможны ложные движения",
            )
        return


_FALLBACK_SYMBOL_PRICE_PRECISION: dict[str, int] = {
    # Common alts whose chart tick size is usually finer than 2 decimals.
    "XRP": 4,
    "ADA": 4,
    "DOGE": 5,
}


def _symbol_base(sym: str | None) -> str | None:
    s = (sym or "").strip()
    if not s:
        return None
    # Examples:
    # - "XRP/USDT" -> "XRP"
    # - "XRP/USDT:USDT" -> "XRP"
    # - "XRPUSDT" -> "XRPUSDT" (unknown format; keep)
    s = s.split(":", 1)[0]
    if "/" in s:
        return s.split("/", 1)[0].strip().upper() or None
    return s.strip().upper() or None


def _price_precision_from_exchange(exchange, symbol: str | None) -> int | None:
    if exchange is None or not symbol:
        return None
    try:
        markets = getattr(exchange, "markets", None)
    except Exception:
        markets = None
    if not isinstance(markets, dict) or not markets:
        return None

    candidates = [symbol]
    try:
        candidates.append(_normalize_bybit_swap_symbol(symbol))
    except Exception:
        pass
    for key in candidates:
        try:
            market = markets.get(key)
        except Exception:
            market = None
        if not isinstance(market, dict):
            continue
        try:
            prec = ((market.get("precision") or {}).get("price"))
        except Exception:
            prec = None
        if isinstance(prec, int) and prec >= 0:
            return prec
    return None


def _price_precision(symbol: str | None, *, exchange=None, value_hint: float | None = None) -> int:
    # Rule: for low-price assets (price < 10 USDT), enforce at least 4 decimals
    # regardless of exchange precision (formatting/quantization only).
    min_prec_by_price = 0
    try:
        if value_hint is not None and math.isfinite(float(value_hint)) and abs(float(value_hint)) < 10.0:
            min_prec_by_price = 4
    except Exception:
        min_prec_by_price = 0

    # 1) Prefer exchange market metadata, if already loaded.
    prec = _price_precision_from_exchange(exchange, symbol)
    if prec is not None:
        return max(int(prec), int(min_prec_by_price))
    # Opportunistically use cached Bybit swap exchange if it already has markets loaded.
    try:
        prec = _price_precision_from_exchange(_EXCHANGES.get("bybit_swap"), symbol)
        if prec is not None:
            return max(int(prec), int(min_prec_by_price))
    except Exception:
        pass

    # 2) Fallback to a small symbol-based map (base token).
    base = _symbol_base(symbol)
    if base and base in _FALLBACK_SYMBOL_PRICE_PRECISION:
        return max(int(_FALLBACK_SYMBOL_PRICE_PRECISION[base]), int(min_prec_by_price))

    # 3) Final fallback: legacy heuristic by magnitude.
    av = abs(float(value_hint)) if value_hint is not None else 0.0
    prec = 2 if av >= 1 else (4 if av >= 0.01 else 6)
    return max(int(prec), int(min_prec_by_price))


def _round_price(val, *, symbol: str | None = None, exchange=None):
    try:
        v = float(val)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    prec = _price_precision(symbol, exchange=exchange, value_hint=v)
    return round(v, prec)

def _round_price_dir(val, direction: str, *, symbol: str | None = None, exchange=None) -> float | None:
    """
    Directional rounding on the same scale as _round_price():
      - direction="down": round towards -inf
      - direction="up":   round towards +inf
      - otherwise: nearest (round)
    """
    try:
        v = float(val)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    prec = _price_precision(symbol, exchange=exchange, value_hint=v)
    m = 10**prec
    if direction == "down":
        return math.floor(v * m) / m
    if direction == "up":
        return math.ceil(v * m) / m
    return round(v, prec)


def quantize_price_levels_to_symbol_precision(d: dict, *, exchange=None) -> dict:
    """
    Hard normalization layer (source of truth):
    quantize all LLM-produced price levels to the exchange symbol price precision.

    This intentionally does not change trading logic; it only rounds price levels.
    """
    if not isinstance(d, dict):
        return d

    symbol = d.get("symbol")

    def _q_price_val(v):
        r = _round_price(v, symbol=symbol, exchange=exchange)
        return float(r) if r is not None else None

    def _q_inplace(container: dict, key: str) -> None:
        if not isinstance(container, dict) or key not in container:
            return
        v = container.get(key)
        r = _q_price_val(v)
        if r is not None:
            container[key] = r

    # Entry prices (single prices, not ranges).
    for k in ("entry_price_neutral", "entry_price_aggressive", "entry_price_conservative"):
        _q_inplace(d, k)

    # Top-level SL/TPs (active mode).
    for k in ("sl", "tp1", "tp2", "tp3"):
        _q_inplace(d, k)

    # Per-mode SL.
    sl_by_mode = d.get("sl_by_mode")
    if isinstance(sl_by_mode, dict):
        for mode_key in list(sl_by_mode.keys()):
            v = sl_by_mode.get(mode_key)
            r = _q_price_val(v)
            if r is not None:
                sl_by_mode[mode_key] = r

    # Per-mode TP buckets.
    tp_by_mode = d.get("tp_by_mode")
    if isinstance(tp_by_mode, dict):
        for mode_key, bucket in list(tp_by_mode.items()):
            if not isinstance(bucket, dict):
                continue
            for k in ("tvh1", "tvh2", "tvh3", "tp1", "tp2", "tp3", "tvh2_or_trail"):
                v = bucket.get(k)
                if isinstance(v, str) and v.strip().lower() == "trail":
                    continue
                r = _q_price_val(v)
                if r is not None:
                    bucket[k] = r
            tp_by_mode[mode_key] = bucket

    # Optional aggressive option entry.
    aggressive_option = d.get("aggressive_option")
    if isinstance(aggressive_option, dict):
        _q_inplace(aggressive_option, "entry_price")

    return d

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
    # In general we avoid emitting direction-guard warnings for "enter now" to keep output lean.
    # Aggressive mode is special-cased: we still want the warning so the later validator can
    # discipline "enter now" behavior against explicit EMA structure.
    mode = normalize_mode(d.get("mode"))
    if emode in ("now", "market") and mode != "aggressive":
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


def _mid_from_range(r, *, symbol: str | None = None, exchange=None):
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
        return _round_price((a + b) / 2.0, symbol=symbol, exchange=exchange)
    if isinstance(r, (list, tuple)) and len(r) == 2:
        try:
            a = float(r[0])
            b = float(r[1])
        except Exception:
            return None
        if b < a:
            a, b = b, a
        return _round_price((a + b) / 2.0, symbol=symbol, exchange=exchange)
    return None


def apply_entry_prices_from_ranges(d: dict) -> None:
    symbol = d.get("symbol")
    side = (d.get("side") or d.get("direction") or "").strip().lower()

    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    neutral_range = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None
    if not isinstance(neutral_range, dict):
        neutral_range = (entries.get("neutral") or {}).get("range") if isinstance(entries.get("neutral"), dict) else None

    aggressive_range = (
        (entries.get("aggressive") or {}).get("range") if isinstance(entries.get("aggressive"), dict) else None
    )
    conservative_range = (
        (entries.get("conservative") or {}).get("range") if isinstance(entries.get("conservative"), dict) else None
    )

    def _range_minmax(r: dict | None) -> tuple[float, float] | None:
        if not isinstance(r, dict):
            return None
        a = _to_float(r.get("min"))
        b = _to_float(r.get("max"))
        if a is None or b is None:
            return None
        if b < a:
            a, b = b, a
        if not (a < b):
            return None
        return (a, b)

    def _clamp_to(mm: tuple[float, float] | None, v: float | None) -> float | None:
        if mm is None or v is None:
            return v
        lo, hi = mm
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    def _pick_neutral_anchor(mm: tuple[float, float] | None) -> float | None:
        if mm is None:
            return None
        lo, hi = mm
        return lo if side == "long" else hi

    def _pick_aggressive_anchor(mm: tuple[float, float] | None) -> float | None:
        if mm is None:
            return None
        lo, hi = mm
        return hi if side == "long" else lo

    if side not in ("long", "short"):
        neutral_mid = _mid_from_range(neutral_range, symbol=symbol)
        if neutral_mid is None:
            neutral_mid = _mid_from_range(((entries.get("neutral") or {}).get("range")), symbol=symbol)
        agg_mid = _mid_from_range(aggressive_range, symbol=symbol)
        cons_mid = _mid_from_range(conservative_range, symbol=symbol)
        if agg_mid is None:
            agg_mid = neutral_mid
        if cons_mid is None:
            cons_mid = neutral_mid
        d["entry_price_neutral"] = _round_price(neutral_mid, symbol=symbol)
        d["entry_price_aggressive"] = _round_price(agg_mid, symbol=symbol)
        d["entry_price_conservative"] = _round_price(cons_mid, symbol=symbol)
        return

    neu_mm = _range_minmax(neutral_range)
    agg_mm = _range_minmax(aggressive_range)
    cons_mm = _range_minmax(conservative_range)

    neutral_anchor = _pick_neutral_anchor(neu_mm)
    aggressive_anchor = _pick_aggressive_anchor(agg_mm) if agg_mm is not None else _pick_aggressive_anchor(neu_mm)
    conservative_anchor = _pick_neutral_anchor(cons_mm) if cons_mm is not None else neutral_anchor

    # Directional rounding so neutral is naturally more conservative than aggressive on the same price scale.
    neu_dir = "down" if side == "long" else "up"
    agg_dir = "up" if side == "long" else "down"
    cons_dir = neu_dir

    neutral_val = _round_price_dir(neutral_anchor, neu_dir, symbol=symbol)
    aggressive_val = _round_price_dir(aggressive_anchor, agg_dir, symbol=symbol)
    conservative_val = _round_price_dir(conservative_anchor, cons_dir, symbol=symbol)

    neutral_val = _clamp_to(neu_mm, neutral_val)
    aggressive_val = _clamp_to(agg_mm, aggressive_val)
    conservative_val = _clamp_to(cons_mm, conservative_val)

    d["entry_price_neutral"] = neutral_val
    d["entry_price_aggressive"] = aggressive_val if aggressive_val is not None else neutral_val
    d["entry_price_conservative"] = conservative_val if conservative_val is not None else neutral_val


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
        ref = _as_float(_mid_from_range(d.get("entry_range"), symbol=d.get("symbol")))
    if ref is None:
        entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
        ref = _as_float(_mid_from_range(((entries.get("neutral") or {}).get("range")), symbol=d.get("symbol")))
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

    symbol = d.get("symbol")
    d["entry_price_aggressive"] = _round_price(agg, symbol=symbol)
    d["entry_price_neutral"] = _round_price(neu, symbol=symbol)
    d["entry_price_conservative"] = _round_price(cons, symbol=symbol)


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
    symbol = d.get("symbol")

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
        mid = _mid_from_range(bucket.get("range"), symbol=symbol)
        if mid is None:
            mid = _mid_from_range(d.get("entry_range"), symbol=symbol)
        if mid is not None:
            d[k] = _round_price(mid, symbol=symbol)
            return float(mid)

        px = _as_float(d.get("price"))
        if px is not None:
            d[k] = _round_price(px, symbol=symbol)
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
        rounded_sl = _round_price(sl_val, symbol=symbol)
        sl_val = float(rounded_sl if rounded_sl is not None else sl_val)
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

            rounded_tvh1 = _round_price(tvh1, symbol=symbol)
            rounded_tvh2 = _round_price(tvh2, symbol=symbol)
            tvh1 = float(rounded_tvh1 if rounded_tvh1 is not None else tvh1)
            tvh2 = float(rounded_tvh2 if rounded_tvh2 is not None else tvh2)
            tvh3_val = _as_float(tvh3)
            rounded_tvh3 = _round_price(tvh3_val, symbol=symbol) if tvh3_val is not None else None
            tvh3 = float(rounded_tvh3 if rounded_tvh3 is not None else tvh3_val) if tvh3_val is not None else None

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
            rounded_tvh1 = _round_price(tvh1, symbol=symbol)
            rounded_tvh2 = _round_price(tvh2, symbol=symbol)
            tvh1 = float(rounded_tvh1 if rounded_tvh1 is not None else tvh1)
            tvh2 = float(rounded_tvh2 if rounded_tvh2 is not None else tvh2)
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
            rounded_tvh1 = _round_price(tvh1, symbol=symbol)
            tvh1 = float(rounded_tvh1 if rounded_tvh1 is not None else tvh1)
            tvh1, _, _ = _ensure_monotonic(entry, tvh1, tvh1, None)

            if tvh2_or_trail is None:
                tvh2_or_trail = "trail"
            elif isinstance(tvh2_or_trail, (int, float)):
                rounded_tvh2_or_trail = _round_price(tvh2_or_trail, symbol=symbol)
                tvh2_or_trail = float(rounded_tvh2_or_trail if rounded_tvh2_or_trail is not None else tvh2_or_trail)
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
        if active_mode == "aggressive":
            d.setdefault("warnings", [])
            if isinstance(d.get("warnings"), list) and "low_rr_aggressive" not in d["warnings"]:
                d["warnings"].append("low_rr_aggressive")
            # В aggressive это не блокер: снижаем ожидания, просим подтверждение, но не выключаем сигнал.
            if (d.get("entry_mode") or "").strip().lower() == "now":
                d["entry_mode"] = "wait_confirm"
            if d.get("confidence") not in ("Low", "Medium", "High"):
                d["confidence"] = "Low"
        else:
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
        # Even when blocked upstream, conservative keeps a fixed horizon contract.
        if normalize_mode(d.get("mode")) == "conservative":
            d["intended_horizon_hours"] = {"min": 24, "max": 72}
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

    # ---- Aggressive extreme blockers + disciplined countertrend handling ----
    if final_mode == "aggressive" and not bool(d.get("no_trade")):
        if bool(d.get("risk_off")):
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and "risk_off" not in reasons:
                reasons.append("risk_off")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "risk_off"
            return d

        side = (d.get("side") or d.get("direction") or "").strip().lower()
        vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
        fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
        fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()

        def _has_reversal_evidence() -> bool:
            if side == "short":
                return bool((fan_h1 == "mixed") or (fan_m15 == "bear") or (vs_h1 != "above"))
            if side == "long":
                return bool((fan_h1 == "mixed") or (fan_m15 == "bull") or (vs_h1 != "below"))
            return False

        # Explicit EMA direction guard: aggressive can be earlier, but must not "enter now"
        # against EMA structure without reversal confirmation.
        try:
            warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else []
            has_dir_guard = "dir_guard_forced_short_by_ema" in warnings
        except Exception:
            has_dir_guard = False
        if side == "long" and has_dir_guard:
            d.setdefault("warnings", [])
            note = "Направление против EMA-структуры — требуется подтверждение разворота."
            if isinstance(d.get("warnings"), list) and note not in d["warnings"]:
                d["warnings"].append(note)
            if not _has_reversal_evidence():
                em = (d.get("entry_mode") or "").strip().lower()
                if em in ("now", "market"):
                    d["entry_mode"] = "wait_confirm"

        if side in ("long", "short") and _m15_flush_detected(d) and not _has_reversal_evidence():
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and "flush_knife_aggressive_extreme" not in reasons:
                reasons.append("flush_knife_aggressive_extreme")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "flush_knife_aggressive_extreme"
            return d

        strong_h1_up = (vs_h1 == "above") and (fan_h1 == "bull")
        strong_h1_down = (vs_h1 == "below") and (fan_h1 == "bear")
        countertrend = (side == "short" and strong_h1_up) or (side == "long" and strong_h1_down)
        if countertrend and not _has_reversal_evidence():
            d.setdefault("warnings", [])
            if isinstance(d.get("warnings"), list) and "countertrend_aggressive_needs_evidence" not in d["warnings"]:
                d["warnings"].append("countertrend_aggressive_needs_evidence")
            em = (d.get("entry_mode") or "").strip().lower()
            if em in ("now", "market"):
                d["entry_mode"] = "wait_confirm"
            if d.get("confidence") == "High":
                d["confidence"] = "Medium"
            if not (d.get("confirmation_rules") or ""):
                d["confirmation_rules"] = (
                    "Контртренд без подтверждения: дождаться разворота EMA-fan на M15 / "
                    "смешанного состояния на H1 / закрепления цены относительно EMA20(H1)."
                )

    # ---- DAY/MID soft-bias override note (intraday facts can dominate) ----
    try:
        ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
        day_bias = (ctx.get("day_bias") or "").strip().lower()
        mid_bias = (ctx.get("mid_bias") or "").strip().lower()
        bias = day_bias if day_bias in ("long", "short") else (mid_bias if mid_bias in ("long", "short") else "")
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
        fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
        strong_contradiction = bool(
            (bias == "long" and vs_h1 == "below" and fan_h1 != "bull")
            or (bias == "short" and vs_h1 == "above" and fan_h1 != "bear")
        )
        if bias in ("long", "short") and side in ("long", "short") and strong_contradiction and side != bias:
            if isinstance(ctx, dict):
                ctx["override_note"] = "⚠️ Расхождение с DAY/MID: intraday структура важнее, торгуем по текущей фазе."
                d["day_mid_context"] = ctx
    except Exception:
        pass

    # ---- Neutral (STRICT): forbid counter-trend completely ----
    # Strong H1 context (existing fields):
    # - Uptrend: price_vs_ema20_h1 == "above" AND ema_fan_h1_state == "bull"
    # - Downtrend: price_vs_ema20_h1 == "below" AND ema_fan_h1_state == "bear"
    if final_mode == "neutral" and not bool(d.get("no_trade")):
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
        fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()

        def _ensure_aggressive_option_from_existing_idea(note: str) -> None:
            existing = d.get("aggressive_option")
            if isinstance(existing, dict) and _to_float(existing.get("entry_price")) is not None:
                existing["note"] = note
                d["aggressive_option"] = existing
                return

            entry = _to_float(d.get("entry_price_aggressive"))
            if entry is None:
                ab = entries.get("aggressive") if isinstance(entries.get("aggressive"), dict) else None
                ar = (ab or {}).get("range") if isinstance(ab, dict) else None
                if isinstance(ar, dict):
                    a_min = _to_float(ar.get("min"))
                    a_max = _to_float(ar.get("max"))
                    if a_min is not None and a_max is not None:
                        entry = (min(a_min, a_max) + max(a_min, a_max)) / 2.0
            if entry is None:
                entry = _to_float(d.get("entry_price_neutral"))
            if entry is None:
                er = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None
                if isinstance(er, dict):
                    e_min = _to_float(er.get("min"))
                    e_max = _to_float(er.get("max"))
                    if e_min is not None and e_max is not None:
                        entry = (min(e_min, e_max) + max(e_min, e_max)) / 2.0
            if entry is None:
                return

            d["aggressive_option"] = {
                "entry_price": float(entry),
                "note": note,
            }

        uptrend_ctx = (vs_h1 == "above") and (fan_h1 == "bull")
        downtrend_ctx = (vs_h1 == "below") and (fan_h1 == "bear")
        forbidden = bool((side == "short" and uptrend_ctx) or (side == "long" and downtrend_ctx))

        if forbidden:
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and "counter_trend_neutral_forbidden" not in reasons:
                reasons.append("counter_trend_neutral_forbidden")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "counter_trend_neutral_forbidden"
            _ensure_aggressive_option_from_existing_idea("Контртрендовая идея — допустима только в aggressive.")
            return d

    # ---- Neutral (STRICT): require stabilization (avoid reversals / knife catches) ----
    if final_mode == "neutral" and not bool(d.get("no_trade")):
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        vs_m15 = str(d.get("price_vs_ema20_m15") or "").strip().lower()
        vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
        fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
        fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
        warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else None

        wrong_side_both = False
        if side == "long":
            wrong_side_both = (vs_m15 == "below") and (vs_h1 == "below")
        elif side == "short":
            wrong_side_both = (vs_m15 == "above") and (vs_h1 == "above")

        if wrong_side_both:
            stabilization = False
            # Conservative interpretation: only count evidence when explicitly present.
            if isinstance(warnings, list) and "impulse_no_exhale" not in warnings:
                stabilization = True
            if side == "long" and fan_m15 and fan_m15 != "bear":
                stabilization = True
            if side == "short" and fan_m15 and fan_m15 != "bull":
                stabilization = True
            if fan_h1 == "mixed":
                stabilization = True

            if not stabilization:
                d["no_trade"] = True
                reasons = d.setdefault("no_trade_reasons", [])
                if isinstance(reasons, list) and "neutral_continuation_unstable_forbidden" not in reasons:
                    reasons.append("neutral_continuation_unstable_forbidden")
                if not (d.get("no_trade_hint") or "").strip():
                    d["no_trade_hint"] = "neutral_continuation_unstable_forbidden"

                existing = d.get("aggressive_option")
                if not (isinstance(existing, dict) and _to_float(existing.get("entry_price")) is not None):
                    entry = _to_float(d.get("entry_price_aggressive"))
                    if entry is None:
                        entry = _to_float(d.get("entry_price_neutral"))
                    if entry is not None:
                        d["aggressive_option"] = {
                            "entry_price": float(entry),
                            "note": "Контртрендовая идея — допустима только в aggressive.",
                        }
                return d

    # ---- Neutral flush-reversal gate (knife-catch forbidden) ----
    # Neutral mode must not take reversal trades right after a sharp M15 flush.
    # Such ideas are allowed only as an aggressive_option.
    if final_mode == "neutral" and not bool(d.get("no_trade")):
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if side in ("long", "short") and _m15_flush_detected(d):
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and "flush_reversal_neutral_forbidden" not in reasons:
                reasons.append("flush_reversal_neutral_forbidden")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "flush_reversal_neutral_forbidden"

            entry_idea = _to_float(d.get("entry_price_neutral"))
            if entry_idea is None:
                er = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None
                if isinstance(er, dict):
                    a = _to_float(er.get("min"))
                    b = _to_float(er.get("max"))
                    if a is not None and b is not None:
                        entry_idea = (min(a, b) + max(a, b)) / 2.0

            if entry_idea is not None:
                existing = d.get("aggressive_option")
                if not isinstance(existing, dict):
                    existing = {}
                existing.setdefault("entry_price", entry_idea)
                existing["note"] = "Разворот после импульсного пролива — допустимо только в aggressive."
                d["aggressive_option"] = existing
            return d

    # ---- Variant B+2: neutral entry must not be too close to current price ----
    # Hard constraint: do not change mode selection rules, SL/TP/RR; only shape neutral entry geometry + transparency.
    if final_mode == "neutral" and not bool(d.get("no_trade")):
        price_val = _to_float(d.get("price"))
        sym = str(d.get("symbol") or "")
        threshold_pct = (
            NEUTRAL_MIN_DIST_PCT_MAJOR
            if (sym.startswith("BTC/") or sym.startswith("ETH/"))
            else NEUTRAL_MIN_DIST_PCT_ALT
        )

        side = (d.get("side") or d.get("direction") or "").strip().lower()
        er = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None
        neu_bucket = entries.get("neutral") if isinstance(entries.get("neutral"), dict) else None
        neu_range_env = (neu_bucket or {}).get("range") if isinstance(neu_bucket, dict) else None

        def _range_to_minmax(r: dict | None) -> tuple[float, float] | None:
            if not isinstance(r, dict):
                return None
            a = _to_float(r.get("min"))
            b = _to_float(r.get("max"))
            if a is None or b is None:
                return None
            if b < a:
                a, b = b, a
            if not (a < b):
                return None
            return (a, b)

        er_mm = _range_to_minmax(er)
        env_mm = _range_to_minmax(neu_range_env)

        neutral_too_close = False
        if price_val is not None and price_val > 0 and er_mm is not None:
            er_mid = (er_mm[0] + er_mm[1]) / 2.0
            dist_pct = abs(er_mid - price_val) / price_val * 100.0
            if dist_pct < threshold_pct:
                neutral_too_close = True

        if neutral_too_close and side in ("long", "short") and env_mm is not None and price_val is not None and price_val > 0:
            env_min, env_max = env_mm
            cur_min, cur_max = er_mm if er_mm is not None else env_mm
            cur_w = cur_max - cur_min
            env_w = env_max - env_min
            if cur_w <= 0:
                cur_w = env_w

            def _clamp_range_minmax(mn: float, mx: float) -> tuple[float, float] | None:
                if not (math.isfinite(mn) and math.isfinite(mx)):
                    return None
                if mx < mn:
                    mn, mx = mx, mn
                if not (mn < mx):
                    return None
                mn = max(mn, env_min)
                mx = min(mx, env_max)
                if not (mn < mx):
                    return None
                return (mn, mx)

            def _adjust_long() -> tuple[float, float] | None:
                target_mid = price_val * (1.0 - threshold_pct / 100.0)
                w = min(cur_w, env_w)
                if w <= 0:
                    return None

                # Prefer keeping width; center at target mid if possible.
                mn0 = target_mid - w / 2.0
                mx0 = target_mid + w / 2.0
                if mn0 >= env_min and mx0 <= env_max:
                    return _clamp_range_minmax(mn0, mx0)

                # Move toward farther (lower) edge inside the envelope.
                mn1 = env_min
                mx1 = env_min + w
                if mx1 > env_max:
                    mx1 = env_max
                mm1 = _clamp_range_minmax(mn1, mx1)
                if mm1 is None:
                    return None
                if (mm1[0] + mm1[1]) / 2.0 <= target_mid:
                    return mm1

                # If still too close, shrink width anchored at the farther edge.
                w2 = 2.0 * (target_mid - env_min)
                if not (math.isfinite(w2) and w2 > 0):
                    return mm1
                w2 = min(w2, env_w)
                if w2 <= 0:
                    return mm1
                return _clamp_range_minmax(env_min, env_min + w2)

            def _adjust_short() -> tuple[float, float] | None:
                target_mid = price_val * (1.0 + threshold_pct / 100.0)
                w = min(cur_w, env_w)
                if w <= 0:
                    return None

                # Prefer keeping width; center at target mid if possible.
                mn0 = target_mid - w / 2.0
                mx0 = target_mid + w / 2.0
                if mn0 >= env_min and mx0 <= env_max:
                    return _clamp_range_minmax(mn0, mx0)

                # Move toward farther (upper) edge inside the envelope.
                mx1 = env_max
                mn1 = env_max - w
                if mn1 < env_min:
                    mn1 = env_min
                mm1 = _clamp_range_minmax(mn1, mx1)
                if mm1 is None:
                    return None
                if (mm1[0] + mm1[1]) / 2.0 >= target_mid:
                    return mm1

                # If still too close, shrink width anchored at the farther edge.
                w2 = 2.0 * (env_max - target_mid)
                if not (math.isfinite(w2) and w2 > 0):
                    return mm1
                w2 = min(w2, env_w)
                if w2 <= 0:
                    return mm1
                return _clamp_range_minmax(env_max - w2, env_max)

            new_mm = _adjust_long() if side == "long" else _adjust_short()
            if new_mm is not None:
                symbol = d.get("symbol")
                rounded_min = _round_price(new_mm[0], symbol=symbol)
                rounded_max = _round_price(new_mm[1], symbol=symbol)
                new_range = {
                    "min": float(rounded_min if rounded_min is not None else new_mm[0]),
                    "max": float(rounded_max if rounded_max is not None else new_mm[1]),
                }

                d["entry_range"] = new_range
                if isinstance(neu_bucket, dict):
                    neu_bucket["range"] = new_range
                    entries["neutral"] = neu_bucket
                    d["entries"] = entries

                neu_mid = _mid_from_range(new_range, symbol=symbol)
                if neu_mid is not None:
                    d["entry_price_neutral"] = neu_mid

                d["neutral_adjusted"] = True
                d["neutral_adjust_reason"] = "neutral_too_close"

                # Optional transparency: expose aggressive option (only from existing computed fields).
                agg_bucket = entries.get("aggressive") if isinstance(entries.get("aggressive"), dict) else None
                if isinstance(agg_bucket, dict) and agg_bucket.get("enabled") is True:
                    agg_range = agg_bucket.get("range") if isinstance(agg_bucket.get("range"), dict) else None
                    agg_mm = _range_to_minmax(agg_range)
                    if agg_mm is not None:
                        agg_entry = _to_float(d.get("entry_price_aggressive"))
                        if agg_entry is None:
                            # Derive from the aggressive mode's own envelope (no shifting).
                            lo, hi = agg_mm
                            anchor = hi if side == "long" else lo
                            agg_entry = _round_price_dir(
                                anchor,
                                "up" if side == "long" else "down",
                                symbol=d.get("symbol"),
                            )

                        neutral_entry = _to_float(d.get("entry_price_neutral"))
                        strict_ok = (
                            (side == "long" and neutral_entry is not None and agg_entry is not None and neutral_entry < agg_entry)
                            or (side == "short" and neutral_entry is not None and agg_entry is not None and neutral_entry > agg_entry)
                        )

                        if strict_ok:
                            d["aggressive_option"] = {
                                "entry_price": agg_entry,
                                "note": "Возможен более ранний вход (aggressive) при повышенном риске.",
                            }

        # ---- Neutral volatility-aware entry spacing (adaptive calmer entry) ----
        # Applies only to entry_price_neutral geometry relative to current price; does not touch EMA/direction/SL/TP/RR math.
        if final_mode == "neutral" and not bool(d.get("no_trade")):
            side = (d.get("side") or d.get("direction") or "").strip().lower()
            px = _to_float(d.get("price"))
            neu0 = _to_float(d.get("entry_price_neutral"))
            if side in ("long", "short") and px is not None and px > 0 and neu0 is not None:
                atr14, range_pct = _m15_volatility_metrics(d)
                d["atr14_m15"] = float(atr14) if atr14 is not None else None
                if range_pct is not None:
                    d["vol_m15"] = float(range_pct)

                is_major = _symbol_is_major(str(d.get("symbol") or ""))
                min_pct = NEUTRAL_VOL_MIN_OFFSET_PCT_MAJOR if is_major else NEUTRAL_VOL_MIN_OFFSET_PCT_ALT
                k_atr = NEUTRAL_VOL_K_ATR_MAJOR if is_major else NEUTRAL_VOL_K_ATR_ALT

                base_abs = float(min_pct) * float(px)
                vol_abs = None
                if atr14 is not None and math.isfinite(float(atr14)) and float(atr14) > 0:
                    vol_abs = float(atr14)
                elif range_pct is not None and math.isfinite(float(range_pct)) and float(range_pct) > 0:
                    vol_abs = float(range_pct) * float(px)

                offset_abs = base_abs if vol_abs is None else max(base_abs, float(k_atr) * float(vol_abs))
                if not (math.isfinite(offset_abs) and offset_abs > 0):
                    offset_abs = base_abs

                d["neutral_offset_abs"] = float(offset_abs)
                d["neutral_offset_pct"] = float(offset_abs) / float(px) * 100.0

                target_from_current = float(px) - float(offset_abs) if side == "long" else float(px) + float(offset_abs)
                cand_raw = (
                    min(float(neu0), target_from_current) if side == "long" else max(float(neu0), target_from_current)
                )
                cand = _round_price_dir(cand_raw, "down" if side == "long" else "up", symbol=d.get("symbol"))
                if cand is None:
                    cand = cand_raw

                sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
                sl_neutral = _to_float(sl_by_mode.get("neutral"))

                def _range_to_minmax(r: dict | None) -> tuple[float, float] | None:
                    if not isinstance(r, dict):
                        return None
                    a = _to_float(r.get("min"))
                    b = _to_float(r.get("max"))
                    if a is None or b is None:
                        return None
                    if b < a:
                        a, b = b, a
                    if not (a < b):
                        return None
                    return (a, b)

                mm = _range_to_minmax(d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None)

                invalid = False
                if not (math.isfinite(float(cand)) and float(cand) > 0):
                    invalid = True
                if mm is not None:
                    lo, hi = mm
                    if float(cand) < float(lo) or float(cand) > float(hi):
                        invalid = True
                if sl_neutral is None or not (math.isfinite(float(sl_neutral)) and float(sl_neutral) > 0):
                    invalid = True
                if not invalid:
                    if side == "long" and float(cand) <= float(sl_neutral):
                        invalid = True
                    if side == "short" and float(cand) >= float(sl_neutral):
                        invalid = True

                if invalid:
                    warnings = d.setdefault("warnings", [])
                    if isinstance(warnings, list) and "neutral_offset_skipped_unsafe" not in warnings:
                        warnings.append("neutral_offset_skipped_unsafe")
                else:
                    d["entry_price_neutral"] = float(cand)

        # ---- Neutral buffer vs aggressive option (soft shaping) ----
        # When neutral mode is active and an aggressive option exists, keep neutral entry
        # meaningfully farther than the aggressive entry by a small tick-based buffer.
        if final_mode == "neutral" and not bool(d.get("no_trade")):
            aggressive_option = d.get("aggressive_option")
            if isinstance(aggressive_option, dict):
                side = (d.get("side") or d.get("direction") or "").strip().lower()
                symbol = d.get("symbol")
                a_entry = _to_float(aggressive_option.get("entry_price"))
                if a_entry is None:
                    a_entry = _to_float(d.get("entry_price_aggressive"))
                n_entry = _to_float(d.get("entry_price_neutral"))
                if side in ("long", "short") and a_entry is not None and n_entry is not None:
                    a_entry_q = _round_price(a_entry, symbol=symbol)
                    if a_entry_q is None:
                        a_entry_q = a_entry
                    n_entry_q = _round_price(n_entry, symbol=symbol)
                    if n_entry_q is None:
                        n_entry_q = n_entry

                    prec = _price_precision(symbol, value_hint=a_entry_q)
                    tick = 10 ** (-int(prec))
                    buffer_val = float(NEUTRAL_BUFFER_TICKS) * float(tick)

                    def _range_to_minmax(r: dict | None) -> tuple[float, float] | None:
                        if not isinstance(r, dict):
                            return None
                        a = _to_float(r.get("min"))
                        b = _to_float(r.get("max"))
                        if a is None or b is None:
                            return None
                        if b < a:
                            a, b = b, a
                        if not (a < b):
                            return None
                        return (a, b)

                    mm = _range_to_minmax(d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None)

                    sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
                    sl_neutral = _to_float(sl_by_mode.get("neutral"))

                    def _candidate_ok(px: float) -> bool:
                        if not (math.isfinite(px) and px > 0):
                            return False
                        if mm is not None:
                            lo, hi = mm
                            if px < lo or px > hi:
                                return False
                        if sl_neutral is None or not math.isfinite(sl_neutral):
                            return False
                        if side == "long" and px <= sl_neutral:
                            return False
                        if side == "short" and px >= sl_neutral:
                            return False
                        return True

                    # Only adjust if neutral is too close to aggressive relative to the buffer.
                    if side == "long":
                        max_neutral = float(a_entry_q) - buffer_val
                        if float(n_entry_q) > max_neutral:
                            cand = _round_price_dir(max_neutral, "down", symbol=symbol)
                            if cand is not None and _candidate_ok(float(cand)):
                                d["entry_price_neutral"] = float(cand)
                    else:  # short
                        min_neutral = float(a_entry_q) + buffer_val
                        if float(n_entry_q) < min_neutral:
                            cand = _round_price_dir(min_neutral, "up", symbol=symbol)
                            if cand is not None and _candidate_ok(float(cand)):
                                d["entry_price_neutral"] = float(cand)

        # ---- Neutral near-market risk gate (STRICT) ----
        # Neutral must be continuation/patient only; near-market entries belong to aggressive.
        try:
            side = (d.get("side") or d.get("direction") or "").strip().lower()
            px = _to_float(d.get("price"))
            n_entry = _to_float(d.get("entry_price_neutral"))
            symbol = d.get("symbol")
            if side in ("long", "short") and px is not None and px > 0 and n_entry is not None:
                prec = _price_precision(symbol, value_hint=px)
                tick_size = 10 ** (-int(prec))
                if tick_size > 0:
                    dist_ticks = abs(float(n_entry) - float(px)) / float(tick_size)
                else:
                    dist_ticks = float("inf")

                if dist_ticks <= float(NEUTRAL_NEAR_TICKS):
                    d["no_trade"] = True
                    reasons = d.setdefault("no_trade_reasons", [])
                    if isinstance(reasons, list) and "neutral_too_close_risky" not in reasons:
                        reasons.append("neutral_too_close_risky")
                    if not (d.get("no_trade_hint") or "").strip():
                        d["no_trade_hint"] = "neutral_too_close_risky"

                    existing = d.get("aggressive_option")
                    if not (isinstance(existing, dict) and _to_float(existing.get("entry_price")) is not None):
                        a_entry = _to_float(d.get("entry_price_aggressive"))
                        if a_entry is None:
                            a_entry = float(n_entry)
                        d["aggressive_option"] = {
                            "entry_price": float(a_entry),
                            "note": "Слишком близко к рынку — допустимо только в aggressive.",
                        }
                    return d
        except Exception:
            pass

    # ---- Conservative: true MID-term, high-confidence gate (MID -> DAY -> local) ----
    if final_mode == "conservative":
        # JSON contract for the mode horizon (even if blocked).
        # Some pipelines pre-fill the key with null; treat that as missing.
        if d.get("intended_horizon_hours") != {"min": 24, "max": 72}:
            d["intended_horizon_hours"] = {"min": 24, "max": 72}

        # Conservative should not auto-suggest aggressive options (even when blocked).
        try:
            d.pop("aggressive_option", None)
        except Exception:
            pass

    if final_mode == "conservative" and not bool(d.get("no_trade")):

        def _block_cons(reason: str, hint: str) -> dict:
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and reason not in reasons:
                reasons.append(reason)
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = reason if reason else hint
            return d

        ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
        mid_bias = str((ctx or {}).get("mid_bias") or "").strip().lower()
        if mid_bias not in ("long", "short"):
            return _block_cons(
                "conservative_requires_mid_bias",
                "Conservative требует явный MID bias (long/short).",
            )

        # MID alignment (hard): the planned side must match MID bias.
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if side in ("long", "short") and side != mid_bias:
            return _block_cons(
                "conservative_requires_mid_bias",
                "Направление не совпадает с MID bias — conservative пропускает.",
            )

        # DAY alignment: same as MID or neutral (not opposite).
        day_bias = str((ctx or {}).get("day_bias") or "").strip().lower()
        if day_bias in ("long", "short") and day_bias != mid_bias:
            return _block_cons(
                "conservative_day_mid_conflict",
                "DAY bias противоречит MID — conservative пропускает.",
            )

        # Local stabilization (hard): no flush/knife + no impulse-no-exhale + no adverse fan.
        warnings = d.get("warnings")
        if isinstance(warnings, list):
            wl = " ".join(str(w or "").strip().lower() for w in warnings)
            if "impulse_no_exhale" in wl:
                return _block_cons(
                    "conservative_local_not_stable",
                    "Локально нет стабилизации после импульса (impulse_no_exhale).",
                )

        if _m15_flush_detected(d):
            return _block_cons(
                "conservative_local_not_stable",
                "Локально риск flush/knife — conservative пропускает.",
            )

        adverse_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
        adverse_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
        if mid_bias == "long":
            if adverse_m15 == "bear" or adverse_h1 == "bear":
                return _block_cons(
                    "conservative_local_not_stable",
                    "EMA fan против направления (bear для LONG) — conservative пропускает.",
                )
        else:
            if adverse_m15 == "bull" or adverse_h1 == "bull":
                return _block_cons(
                    "conservative_local_not_stable",
                    "EMA fan против направления (bull для SHORT) — conservative пропускает.",
                )

        # Entry placement (hard): deeper than neutral and not near-market.
        price_val = _to_float(d.get("price"))
        symbol = str(d.get("symbol") or "")

        cons_entry = _to_float(d.get("entry_price_conservative"))
        if cons_entry is None:
            entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
            cb = entries.get("conservative") if isinstance(entries.get("conservative"), dict) else {}
            cons_entry = _mid_from_range(cb.get("range"), symbol=symbol)

        neu_entry = _to_float(d.get("entry_price_neutral"))
        if neu_entry is None:
            entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
            nb = entries.get("neutral") if isinstance(entries.get("neutral"), dict) else {}
            neu_entry = _mid_from_range(nb.get("range"), symbol=symbol)

        if cons_entry is not None and neu_entry is not None:
            deeper_ok = (mid_bias == "long" and cons_entry < neu_entry) or (
                mid_bias == "short" and cons_entry > neu_entry
            )
            if not deeper_ok:
                return _block_cons(
                    "conservative_entry_too_close",
                    "Conservative-вход должен быть глубже neutral.",
                )

        if price_val is not None and price_val > 0 and cons_entry is not None:
            threshold_pct = (
                NEUTRAL_MIN_DIST_PCT_MAJOR
                if (symbol.startswith("BTC/") or symbol.startswith("ETH/"))
                else NEUTRAL_MIN_DIST_PCT_ALT
            )
            dist_pct = abs(cons_entry - price_val) / price_val * 100.0
            if dist_pct < float(threshold_pct):
                return _block_cons(
                    "conservative_entry_too_close",
                    "Conservative-вход слишком близко к текущей цене.",
                )

        # If conservative uses trail as the farther target, explicitly set the 1–3 day horizon in text.
        try:
            tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
            tp_bucket = tp_by_mode.get("conservative") if isinstance(tp_by_mode.get("conservative"), dict) else {}
            tvh2_or_trail = tp_bucket.get("tvh2_or_trail")
            is_trail = isinstance(tvh2_or_trail, str) and tvh2_or_trail.strip().lower() == "trail"
            if is_trail:
                ep_by_mode = d.get("exit_plan_by_mode") if isinstance(d.get("exit_plan_by_mode"), dict) else {}
                txt = str(ep_by_mode.get("conservative") or "").strip()
                low = txt.lower()
                if txt and ("1–3" not in txt and "1-3" not in txt and "дн" not in low and "24" not in low and "72" not in low):
                    ep_by_mode["conservative"] = (txt + " Горизонт: 1–3 дня.").strip()
                    d["exit_plan_by_mode"] = ep_by_mode
        except Exception:
            pass

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
    requested_mode = normalize_mode(mode_val)
    d["mode"] = requested_mode
    # Persist the originally requested mode for transparency (may differ from final mode after fallback).
    d["requested_mode"] = requested_mode

    d.setdefault("warnings", [])
    if not d.get("time_msk"):
        d["time_msk"] = current_msk()

    if fetch_price and symbol and not DRY_RUN and not d.get("price"):
        try:
            ticker = get_pair_ticker(symbol)
            rounded = _round_price(ticker.get("last"), symbol=symbol)
            if rounded is not None:
                d["price"] = rounded
        except Exception:
            pass

    # EMA20(M15/H1) must be derived from computed OHLCV provenance (Bybit linear perp),
    # never from LLM-provided fields or stale hints.
    try:
        overwrite_ema20_from_provenance(d)
    except Exception:
        pass

    def _refresh_price() -> float | None:
        if not symbol:
            return None
        try:
            return _round_price(get_pair_ticker(symbol).get("last"), symbol=symbol)
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

    # Conservative direction: follow MID bias (primary) to avoid long/short default bias.
    try:
        if normalize_mode(d.get("mode")) == "conservative":
            ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
            mid_bias = str((ctx or {}).get("mid_bias") or "").strip().lower()
            if mid_bias in ("long", "short"):
                d["side"] = mid_bias
    except Exception:
        pass

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
    except Exception:
        pass

    apply_ema_exhale_filter(d)

    validate_or_fallback_tvh_by_mode(d)
    _debug_trace_set_entry_range("entry_range_post_tvh", d)
    try:
        symbol = d.get("symbol")
        final_mode = normalize_mode(d.get("mode"))

        sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
        sl_val = _to_float(sl_by_mode.get(final_mode))
        if sl_val is not None and sl_val:
            rounded = _round_price(sl_val, symbol=symbol)
            d["sl"] = float(rounded if rounded is not None else sl_val)

        tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
        tp_bucket = tp_by_mode.get(final_mode) if isinstance(tp_by_mode.get(final_mode), dict) else {}

        tp1_val = _to_float(tp_bucket.get("tvh1"))
        if tp1_val is not None and tp1_val:
            rounded = _round_price(tp1_val, symbol=symbol)
            d["tp1"] = float(rounded if rounded is not None else tp1_val)

        tp2_val = _to_float(tp_bucket.get("tvh2"))
        if tp2_val is None:
            tvh2_or_trail = tp_bucket.get("tvh2_or_trail")
            tp2_val = _to_float(tvh2_or_trail)
        if tp2_val is not None and tp2_val:
            rounded = _round_price(tp2_val, symbol=symbol)
            d["tp2"] = float(rounded if rounded is not None else tp2_val)

        tp3_val = _to_float(tp_bucket.get("tvh3"))
        if tp3_val is not None and tp3_val:
            rounded = _round_price(tp3_val, symbol=symbol)
            d["tp3"] = float(rounded if rounded is not None else tp3_val)
    except Exception:
        pass

    # Phase-flip modifier (micro-phase change): can pause neutral or force aggressive wait_confirm.
    try:
        apply_phase_flip_modifier(d)
    except Exception:
        pass

    validate_active_mode_setup(d)
    try:
        quantize_price_levels_to_symbol_precision(d)
    except Exception:
        pass
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
