#!/usr/bin/env python3
import os
import sys
import json
import math
import time
import re
import argparse
import copy
from datetime import datetime, timedelta
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
from event_risk_context import (
    attach_event_risk_context,
    build_event_risk_context,
    merge_event_risk_snapshots,
    normalize_event_risk_snapshot,
    render_event_risk_context_section,
)
from event_calendar import (
    build_calendar_context,
    build_calendar_risk_summary,
    render_calendar_section,
)

VALID_MODES = {"aggressive", "neutral", "conservative"}
VALID_HOLDING_HORIZONS = {"intraday", "intraday_to_1_2d", "short_swing", "multi_day"}
DEFAULT_OPENAI_MODEL = "gpt-5.2"
FIXED_BOT_ASSET_UNIVERSE = frozenset({"BTC", "ETH", "BNB", "SOL", "XRP"})
FLOW_STALENESS_LIMITS_MINUTES = {
    "day": 60.0,
    "mid": 180.0,
    "signal": 60.0,
}

# ---- Variant B+2: neutral semantics guard ----
# Neutral mode must not recommend entries "too close" to current price.
# Thresholds are configurable here (single place).
NEUTRAL_MIN_DIST_PCT_MAJOR = 0.25  # BTC/, ETH/
NEUTRAL_MIN_DIST_PCT_ALT = 0.35  # others

# ---- Neutral volatility-aware spacing (adaptive calmer entry) ----
# Applies only to entry_price_neutral placement; does not change EMA/provenance, direction, SL/TP/RR math, or gates.
NEUTRAL_VOL_MIN_OFFSET_PCT_MAJOR = 0.006  # 0.60%
NEUTRAL_VOL_MIN_OFFSET_PCT_ALT = 0.008  # 0.80%
NEUTRAL_VOL_K_ATR_MAJOR = 1.0
NEUTRAL_VOL_K_ATR_ALT = 1.1

# When neutral mode exposes an early aggressive entry option, keep neutral meaningfully farther.
# Soft shaping only: does not hard-limit the model or change EMA/no_trade/direction logic.
NEUTRAL_BUFFER_TICKS = 10

# Near-market definition (neutral): if neutral entry is within this many ticks from current price,
# and no aggressive_option exists, split into (aggressive_option=original) + buffered neutral entry.
NEUTRAL_NEAR_TICKS = 20

# Published signal quality floor:
# TP1 must provide at least 1% clean movement from the selected entry anchor.
TP1_MIN_NET_MOVE_PCT = 0.01

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


def _normalize_holding_horizon(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "intraday_to_12d": "intraday_to_1_2d",
        "intraday_1_2d": "intraday_to_1_2d",
        "intraday_to_1_2_days": "intraday_to_1_2d",
        "intraday_to_2d": "intraday_to_1_2d",
        "shortswing": "short_swing",
        "swing_short": "short_swing",
        "multi": "multi_day",
        "multiday": "multi_day",
    }
    normalized = aliases.get(text, text)
    return normalized if normalized in VALID_HOLDING_HORIZONS else None


def _holding_horizon_label(value) -> str:
    horizon = _normalize_holding_horizon(value) or ""
    return {
        "intraday": "intraday",
        "intraday_to_1_2d": "intraday / 1–2 дня",
        "short_swing": "1–3 дня / short swing",
        "multi_day": "2–5 дней / multi-day",
    }.get(horizon, "по структуре сетапа")


def _mode_holding_horizon(mode_value, entry_mode_value=None) -> str | None:
    mode = normalize_mode(mode_value)
    if mode == "aggressive":
        return "intraday_to_1_2d"
    if mode == "neutral":
        return "short_swing"
    if mode == "conservative":
        return "multi_day"
    return None


def _derive_holding_horizon(d: dict) -> str:
    contracted = _mode_holding_horizon(d.get("mode"), d.get("entry_mode"))
    if contracted:
        return contracted

    explicit = _normalize_holding_horizon(d.get("holding_horizon"))
    if explicit:
        return explicit
    return "short_swing"


def _apply_holding_horizon_contract(d: dict) -> None:
    if not isinstance(d, dict):
        return
    horizon = _derive_holding_horizon(d)
    d["holding_horizon"] = horizon
    d["holding_horizon_label"] = _holding_horizon_label(horizon)


def _canonical_invalidation_phrase(side: str) -> str:
    if side == "short":
        return "отмена сценария: уход выше EMA60(M15) / структурного high"
    return "отмена сценария: уход ниже EMA60(M15) / структурного low"


def _sanitize_invalidation_text(text, *, side: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = text.strip()
    if not cleaned or side not in {"long", "short"}:
        return cleaned

    low = cleaned.lower()
    has_invalidation_context = any(
        needle in low
        for needle in (
            "invalidate",
            "invalidat",
            "отмена сценария",
            "сценарий отмен",
        )
    )
    has_structural_anchor = any(
        needle in low
        for needle in (
            "ema60",
            "ema 60",
            "m15",
            "м15",
            "structural",
            "структур",
            "support",
            "resistance",
            " low",
            " high",
            "лой",
            "лоя",
            "хай",
            "хая",
        )
    )

    if side == "long":
        conflicting_direction = bool(
            re.search(
                r"(?i)уход\s+выше[^.;,\n]{0,120}(?:ema\s*60|support|low|лой|лоя|structural|структур)",
                cleaned,
            )
        )
    else:
        conflicting_direction = bool(
            re.search(
                r"(?i)уход\s+ниже[^.;,\n]{0,120}(?:ema\s*60|resistance|high|хай|хая|structural|структур)",
                cleaned,
            )
        )

    if not ((has_invalidation_context and has_structural_anchor) or conflicting_direction):
        return cleaned

    canonical = _canonical_invalidation_phrase(side)
    patterns = (
        r"(?i)(?:ч[её]ткий\s+)?invalidate\s*\([^)]*\)",
        r"(?i)(?:ч[её]ткий\s+)?invalidate\s*[:\-–—]?\s*[^.;,\n]{0,140}",
        r"(?i)отмена\s+сценария\s*[:\-–—]?\s*[^.;,\n]{0,140}",
        r"(?i)сценарий\s+отмен(?:яется|ится)\s*[:\-–—]?\s*[^.;,\n]{0,140}",
    )

    out = cleaned
    replaced = False
    for pattern in patterns:
        new_text, count = re.subn(pattern, canonical, out, count=1)
        if count:
            out = new_text
            replaced = True

    if replaced:
        return re.sub(r"\s+", " ", out).strip()
    if conflicting_direction:
        return canonical
    return cleaned


def _sanitize_signal_invalidation_wording(d: dict) -> None:
    if not isinstance(d, dict):
        return
    side_raw = d.get("side")
    side_val = side_raw if isinstance(side_raw, str) and side_raw.strip() else d.get("direction")
    side = str(side_val or "").strip().lower()
    if side not in {"long", "short"}:
        return

    for field in ("why_asset", "technical_rationale", "cancel_condition"):
        value = d.get(field)
        if isinstance(value, dict):
            summary = value.get("summary")
            if isinstance(summary, str):
                value["summary"] = _sanitize_invalidation_text(summary, side=side)
                d[field] = value
        elif isinstance(value, str):
            d[field] = _sanitize_invalidation_text(value, side=side)


def _rewrite_signal_horizon_wording(text, *, horizon_label: str, mode: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    if mode not in {"aggressive", "neutral"}:
        return cleaned

    low = cleaned.lower()
    if "3–7" not in low and "3-7" not in low:
        return cleaned

    replacements = (
        (
            r"(?i)собрать\s+rr\s+для\s+горизонта\s+3[\-–]7\s+дн(?:ей|я)",
            f"собрать RR для тактического сетапа {horizon_label}",
        ),
        (
            r"(?i)для\s+горизонта\s+3[\-–]7\s+дн(?:ей|я)",
            f"для тактического сетапа {horizon_label}",
        ),
        (
            r"(?i)на\s+горизонте\s+3[\-–]7\s+дн(?:ей|я)",
            f"в тактическом горизонте {horizon_label}",
        ),
        (
            r"(?i)горизонт(?:ом)?\s+3[\-–]7\s+дн(?:ей|я)",
            horizon_label,
        ),
    )

    out = cleaned
    for pattern, replacement in replacements:
        out = re.sub(pattern, replacement, out)
    return out


def _sanitize_signal_horizon_wording(d: dict) -> None:
    if not isinstance(d, dict):
        return
    _apply_holding_horizon_contract(d)
    mode = normalize_mode(d.get("mode"))
    horizon_label = str(d.get("holding_horizon_label") or "").strip() or _holding_horizon_label(d.get("holding_horizon"))
    for field in ("why_asset", "technical_rationale"):
        value = d.get(field)
        if isinstance(value, dict):
            summary = value.get("summary")
            if isinstance(summary, str):
                value["summary"] = _rewrite_signal_horizon_wording(summary, horizon_label=horizon_label, mode=mode)
                d[field] = value
        elif isinstance(value, str):
            d[field] = _rewrite_signal_horizon_wording(value, horizon_label=horizon_label, mode=mode)
    _sanitize_signal_invalidation_wording(d)


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
_OHLCV_TAIL_LEN: int = 200


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
        # Keep a reproducible tail of OHLCV for derived calculations + EMA blocks.
        # Format is ccxt OHLCV: [ts, open, high, low, close, volume?]
        "ohlcv_tail": ohlcv[-_OHLCV_TAIL_LEN:] if isinstance(ohlcv, list) else None,
        "fetched_at": time.time(),
        "closes": closes,
        "closes_tail": closes[-_OHLCV_TAIL_LEN:],
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
            out["closes_tail"] = [float(x) for x in closes[-_OHLCV_TAIL_LEN:]]
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

    # Hard requirement: ensure we have enough OHLCV to compute EMA fan blocks deterministically.
    try:
        m15_ok = int(d.get("candles_m15_count") or 0) >= _OHLCV_TAIL_LEN
        h1_ok = int(d.get("candles_h1_count") or 0) >= _OHLCV_TAIL_LEN
    except Exception:
        m15_ok, h1_ok = False, False

    if not (m15_ok and h1_ok and _is_num(d.get("ema20_m15")) and _is_num(d.get("ema20_h1"))):
        d["no_trade"] = True
        reasons = d.setdefault("no_trade_reasons", [])
        if isinstance(reasons, list) and "insufficient_ohlcv_for_ema_fan" not in reasons:
            reasons.append("insufficient_ohlcv_for_ema_fan")
        if not isinstance(d.get("no_trade_hint"), str) or not str(d.get("no_trade_hint") or "").strip():
            d["no_trade_hint"] = "insufficient_ohlcv_for_ema_fan"


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
    _sanitize_signal_horizon_wording(d)
    return d


def normalize_mode(mode_val) -> str:
    try:
        m = (mode_val or "").strip().lower()
    except Exception:
        m = ""
    return m if m in VALID_MODES else "neutral"

def ensure_warnings_list(d: dict) -> None:
    """
    Runtime safety: ensure `warnings` is always a list (some upstream payloads can emit str/null).
    This is plumbing-only (no trading logic changes), but prevents `.setdefault("warnings", []).append(...)`
    from crashing when `warnings` exists with a non-list type.
    """
    if not isinstance(d, dict):
        return
    w = d.get("warnings")
    if isinstance(w, list):
        return
    if isinstance(w, str) and w.strip():
        d["warnings"] = [w.strip()]
    else:
        d["warnings"] = []


def _normalize_optional_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _normalize_optional_number(value):
    num = _to_float(value)
    if num is None or not math.isfinite(num):
        return None
    if float(num).is_integer():
        return int(num)
    return float(num)


def _normalize_upcoming_event_item(item):
    if isinstance(item, str):
        s = item.strip()
        return {"event": s} if s else None

    if not isinstance(item, dict):
        return None

    out: dict = {}
    text_keys = (
        "event",
        "category",
        "impact",
        "time_msk",
        "date_msk",
        "note",
        "expected_regime_effect",
        "type",
    )
    number_keys = ("window_before_min", "window_after_min")

    for key in text_keys:
        if key not in item:
            continue
        text = _normalize_optional_text(item.get(key))
        if text:
            out[key] = text

    if out.get("note"):
        out["note"] = _merge_unique_texts(out.get("note"), max_fragments=2)

    for key in number_keys:
        if key not in item:
            continue
        num = _normalize_optional_number(item.get(key))
        if num is not None:
            out[key] = num

    # Preserve any extra scalar keys so partially useful events are not silently lost.
    for key, value in item.items():
        if key in out or key in text_keys or key in number_keys:
            continue
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, bool):
            text = _normalize_optional_text(value)
            if text:
                out[key] = text
            continue
        if isinstance(value, bool):
            out[key] = value

    return out if out else None


def _normalize_macro_event_bundle(raw_events, raw_summary) -> tuple[list[dict], str]:
    normalized_events: list[dict] = []

    if isinstance(raw_events, list):
        for item in raw_events:
            normalized = _normalize_upcoming_event_item(item)
            if normalized is not None:
                normalized_events.append(normalized)
    elif isinstance(raw_events, dict):
        normalized = _normalize_upcoming_event_item(raw_events)
        if normalized is not None:
            normalized_events.append(normalized)
    elif isinstance(raw_events, str):
        normalized = _normalize_upcoming_event_item(raw_events)
        if normalized is not None:
            normalized_events.append(normalized)

    return normalized_events, _merge_unique_texts(raw_summary, max_fragments=3)


def ensure_macro_event_fields(d: dict) -> None:
    if not isinstance(d, dict):
        return

    normalized_events, normalized_summary = _normalize_macro_event_bundle(
        d.get("upcoming_events"),
        d.get("macro_risk_summary"),
    )
    d["upcoming_events"] = _merge_upcoming_event_lists(normalized_events)
    d["macro_risk_summary"] = _merge_unique_texts(normalized_summary, max_fragments=3)


def _normalize_text_key(value) -> str:
    text = _normalize_optional_text(value).lower()
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _text_word_tokens(value) -> list[str]:
    text = _normalize_text_key(value)
    if not text:
        return []
    return re.findall(r"[0-9a-zа-яё]+", text)


def _normalize_event_identity_text(value) -> str:
    tokens = _text_word_tokens(value)
    if not tokens:
        return ""

    normalized_tokens: list[str] = []
    acronym: list[str] = []
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            acronym.append(token)
            continue
        if acronym:
            normalized_tokens.append("".join(acronym))
            acronym = []
        normalized_tokens.append(token)
    if acronym:
        normalized_tokens.append("".join(acronym))
    return " ".join(normalized_tokens).strip()


_SEMANTIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "could",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "без",
    "ближе",
    "ближайшие",
    "ближайшим",
    "будут",
    "бы",
    "в",
    "во",
    "вокруг",
    "времени",
    "временно",
    "все",
    "всего",
    "для",
    "до",
    "же",
    "за",
    "и",
    "или",
    "из",
    "их",
    "к",
    "ко",
    "как",
    "когда",
    "может",
    "могут",
    "на",
    "над",
    "не",
    "но",
    "о",
    "об",
    "оба",
    "обе",
    "около",
    "он",
    "она",
    "они",
    "от",
    "по",
    "под",
    "после",
    "перед",
    "при",
    "про",
    "с",
    "со",
    "также",
    "то",
    "только",
    "у",
    "это",
    "этот",
    "эта",
    "эти",
}


def _normalize_semantic_token(token: str) -> str:
    text = _normalize_optional_text(token).lower().replace("ё", "е")
    if not text:
        return ""

    if len(text) > 4:
        if text.endswith("ies"):
            text = text[:-3] + "y"
        elif text.endswith("ing"):
            text = text[:-3]
        elif text.endswith("ed"):
            text = text[:-2]
        elif text.endswith("es"):
            text = text[:-2]
        elif text.endswith("s") and not text.endswith("ss"):
            text = text[:-1]

    for suffix in (
        "ировать",
        "ениями",
        "ового",
        "евому",
        "овому",
        "ением",
        "остью",
        "ацией",
        "яцией",
        "ация",
        "яция",
        "иями",
        "ости",
        "ями",
        "ами",
        "ием",
        "иях",
        "ого",
        "ему",
        "ому",
        "ыми",
        "ими",
        "цией",
        "ция",
        "ции",
        "ией",
        "ий",
        "ый",
        "ой",
        "ая",
        "ое",
        "ые",
        "их",
        "ых",
        "ую",
        "юю",
        "ам",
        "ям",
        "ом",
        "ем",
        "ов",
        "ев",
        "ия",
        "ья",
        "ие",
        "ье",
        "ка",
        "ки",
        "ть",
        "ти",
        "а",
        "я",
        "ы",
        "и",
        "е",
        "у",
        "ю",
        "о",
    ):
        if len(text) - len(suffix) >= 3 and text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _semantic_text_tokens(value) -> list[str]:
    out: list[str] = []
    for token in _text_word_tokens(value):
        normalized = _normalize_semantic_token(token)
        if not normalized:
            continue
        if normalized in _SEMANTIC_STOPWORDS:
            continue
        if len(normalized) <= 1 and not normalized.isdigit():
            continue
        out.append(normalized)
    return out


def _semantic_texts_match(left, right) -> bool:
    if _event_texts_match(left, right):
        return True

    left_ordered = _semantic_text_tokens(left)
    right_ordered = _semantic_text_tokens(right)
    left_tokens = set(left_ordered)
    right_tokens = set(right_ordered)
    if not left_tokens or not right_tokens:
        return False

    common = left_tokens & right_tokens
    if not common:
        return False

    min_ratio = len(common) / min(len(left_tokens), len(right_tokens))
    max_ratio = len(common) / max(len(left_tokens), len(right_tokens))
    if min_ratio >= 0.6 or (len(common) >= 4 and max_ratio >= 0.5):
        return True
    if left_ordered[:2] == right_ordered[:2] and len(common) >= 4:
        return True
    return bool(left_ordered and right_ordered) and left_ordered[0] == right_ordered[0] and len(common) >= 6


def _fragment_quality_score(value) -> tuple[int, int]:
    text = _normalize_optional_text(value)
    semantic_count = len(set(_semantic_text_tokens(text)))
    return semantic_count, -len(text)


def _find_matching_fragment_index(fragments: list[str], candidate: str) -> int | None:
    for idx, existing in enumerate(fragments):
        if _semantic_texts_match(existing, candidate):
            return idx
    return None


def _collect_unique_text_fragments(*values, max_fragments: int | None = None) -> list[str]:
    out: list[str] = []
    for value in values:
        fragments = _split_text_fragments(value)
        if not fragments:
            continue
        for fragment in fragments:
            match_idx = _find_matching_fragment_index(out, fragment)
            if match_idx is not None:
                if _fragment_quality_score(fragment) > _fragment_quality_score(out[match_idx]):
                    out[match_idx] = fragment
                continue
            if max_fragments is not None and len(out) >= max_fragments:
                continue
            out.append(fragment)
    return out


def _split_text_fragments(value) -> list[str]:
    text = _normalize_optional_text(value)
    if not text:
        return []

    out: list[str] = []
    seen: set[str] = set()
    chunks = re.split(r"[\r\n]+", text)
    for chunk in chunks:
        piece = chunk.strip()
        if not piece:
            continue
        fragments = re.split(r"(?<=[.!?])\s+|\s*[;•]+\s*", piece)
        for fragment in fragments:
            candidate = fragment.strip()
            if not candidate:
                continue
            key = _normalize_event_identity_text(candidate) or _normalize_text_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def _event_fingerprint(item: dict) -> str:
    fingerprint = _event_primary_identity_key(item)
    if fingerprint:
        return fingerprint

    fallback = (
        _normalize_event_identity_text(item.get("impact")),
        _normalize_event_identity_text(item.get("expected_regime_effect")),
        _normalize_event_identity_text(item.get("type")),
    )
    return "|".join(p for p in fallback if p)


def _event_identity_label(item: dict) -> str:
    return _normalize_event_identity_text(item.get("event")) or _normalize_event_identity_text(item.get("note"))


def _event_primary_identity_key(item: dict) -> str:
    label = _event_identity_label(item)
    category = _normalize_event_identity_text(item.get("category"))
    if label and category:
        return f"{label}|{category}"
    if label:
        return label
    return category


def _extract_date_from_text(value) -> str:
    text = _normalize_optional_text(value)
    match = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", text)
    return match.group(0) if match else ""


def _extract_hhmm_from_text(value) -> str:
    text = _normalize_optional_text(value)
    match = re.search(r"\b\d{1,2}:\d{2}\b", text)
    return match.group(0) if match else ""


def _normalize_event_time_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    time_text = _normalize_optional_text(item.get("time_msk"))
    date_text = _normalize_optional_text(item.get("date_msk")) or _extract_date_from_text(time_text)
    hhmm = _extract_hhmm_from_text(time_text)
    if hhmm:
        return "|".join(part for part in (date_text, hhmm) if part)
    if _event_time_is_tbd(item):
        return f"tbd|{date_text}" if date_text else "tbd"
    normalized = _normalize_event_identity_text(time_text)
    return "|".join(part for part in (date_text, normalized) if part)


def _event_times_match(left_event: dict, right_event: dict) -> bool:
    left_key = _normalize_event_time_key(left_event)
    right_key = _normalize_event_time_key(right_event)
    return (not left_key) or (not right_key) or left_key == right_key


def _event_time_detail_score(time_value, *, date_value=None) -> tuple[int, int]:
    time_text = _normalize_optional_text(time_value)
    date_text = _normalize_optional_text(date_value)
    if not time_text:
        return 0, 0
    if _extract_hhmm_from_text(time_text):
        return 3, len(time_text)
    if re.search(r"\btbd\b", time_text, flags=re.IGNORECASE):
        if _extract_date_from_text(time_text) or date_text:
            return 2, len(time_text)
        return 1, len(time_text)
    if date_text or _extract_date_from_text(time_text):
        return 2, len(time_text)
    return 1, len(time_text)


def _event_date_detail_score(date_value) -> tuple[int, int]:
    date_text = _normalize_optional_text(date_value)
    if not date_text:
        return 0, 0
    normalized = _normalize_event_identity_text(date_text)
    if _extract_date_from_text(date_text):
        return 2, len(normalized)
    if normalized == "tbd":
        return 1, len(normalized)
    return 1, len(normalized)


def _event_fields_compatible(left, right) -> bool:
    left_key = _normalize_event_identity_text(left)
    right_key = _normalize_event_identity_text(right)
    return (not left_key) or (not right_key) or left_key == right_key


def _event_texts_match(left, right) -> bool:
    left_key = _normalize_event_identity_text(left)
    right_key = _normalize_event_identity_text(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True

    shorter, longer = sorted((left_key, right_key), key=len)
    if len(shorter) >= 12 and shorter in longer:
        return True

    left_tokens = set(left_key.split())
    right_tokens = set(right_key.split())
    if not left_tokens or not right_tokens:
        return False
    common = left_tokens & right_tokens
    if bool(common) and (len(common) / min(len(left_tokens), len(right_tokens))) >= 0.8:
        return True

    left_semantic = set(_semantic_text_tokens(left))
    right_semantic = set(_semantic_text_tokens(right))
    if not left_semantic or not right_semantic:
        return False
    semantic_common = left_semantic & right_semantic
    return bool(semantic_common) and (
        (len(semantic_common) / min(len(left_semantic), len(right_semantic))) >= 0.75
    )


def _find_matching_event_index(events: list[dict], candidate: dict, seen: dict[str, int]) -> int | None:
    fingerprint = _event_fingerprint(candidate)
    if fingerprint and fingerprint in seen:
        return seen[fingerprint]

    candidate_label = _event_identity_label(candidate)
    candidate_category = candidate.get("category")

    for idx, existing in enumerate(events):
        if not _event_texts_match(candidate_label, _event_identity_label(existing)):
            continue
        if not _event_fields_compatible(candidate_category, existing.get("category")):
            continue
        return idx
    return None


def _merge_event_values(primary: dict, incoming: dict) -> dict:
    merged = copy.deepcopy(primary)
    canonical_text_keys = (
        "event",
        "category",
        "impact",
        "date_msk",
        "time_msk",
        "note",
        "expected_regime_effect",
        "type",
    )
    canonical_number_keys = ("window_before_min", "window_after_min")

    for key in canonical_text_keys:
        existing = _normalize_optional_text(merged.get(key))
        incoming_val = _normalize_optional_text(incoming.get(key))
        if key == "note" and existing and incoming_val:
            merged[key] = _merge_unique_texts(existing, incoming_val, max_fragments=2)
            continue
        if key == "date_msk" and existing and incoming_val:
            if _event_date_detail_score(incoming_val) > _event_date_detail_score(existing):
                merged[key] = incoming_val
            continue
        if key == "time_msk" and existing and incoming_val:
            existing_score = _event_time_detail_score(existing, date_value=merged.get("date_msk"))
            incoming_score = _event_time_detail_score(incoming_val, date_value=incoming.get("date_msk") or merged.get("date_msk"))
            if incoming_score > existing_score:
                merged[key] = incoming_val
            continue
        if key == "expected_regime_effect" and existing and incoming_val:
            if _fragment_quality_score(incoming_val) > _fragment_quality_score(existing):
                merged[key] = incoming_val
            continue
        if key == "event" and existing and incoming_val and _semantic_texts_match(existing, incoming_val):
            if _fragment_quality_score(incoming_val) > _fragment_quality_score(existing):
                merged[key] = incoming_val
            continue
        if not existing and incoming_val:
            merged[key] = incoming_val

    for key in canonical_number_keys:
        if merged.get(key) is None and incoming.get(key) is not None:
            merged[key] = incoming.get(key)

    for key, value in incoming.items():
        if key in canonical_text_keys or key in canonical_number_keys:
            continue
        if key not in merged or merged.get(key) in (None, "", []):
            merged[key] = copy.deepcopy(value)
            continue
        if (
            isinstance(merged.get(key), str)
            and isinstance(value, str)
            and _normalize_text_key(merged.get(key)) != _normalize_text_key(value)
        ):
            merged[key] = _merge_unique_texts(merged.get(key), value)

    return merged


def _merge_upcoming_event_lists(*groups) -> list[dict]:
    out: list[dict] = []
    seen: dict[str, int] = {}
    for group in groups:
        normalized_events, _ = _normalize_macro_event_bundle(group, "")
        for item in normalized_events:
            match_idx = _find_matching_event_index(out, item, seen)
            if match_idx is not None:
                out[match_idx] = _merge_event_values(out[match_idx], item)
                merged_fp = _event_fingerprint(out[match_idx])
                if merged_fp:
                    seen[merged_fp] = match_idx
                continue
            fp = _event_fingerprint(item)
            if fp:
                seen[fp] = len(out)
            out.append(item)
    return out


def _merge_unique_texts(*values, max_fragments: int | None = None) -> str:
    return " ".join(_collect_unique_text_fragments(*values, max_fragments=max_fragments))


def _summary_fragment_mentions_event(fragment: str, event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    text = _normalize_optional_text(fragment)
    if not text:
        return False

    label = _normalize_optional_text(event.get("event"))
    if label and _semantic_texts_match(text, label):
        return True

    time_text = _event_display_time(event)
    if time_text and time_text in text:
        return True

    date_text = _normalize_optional_text(event.get("date_msk"))
    hhmm = _extract_hhmm_from_text(event.get("time_msk"))
    if date_text and date_text in text:
        return True
    if hhmm and hhmm in text and label:
        return True
    return False


def _is_calendar_summary_fragment(fragment: str, events: list[dict]) -> bool:
    text = _normalize_optional_text(fragment)
    if not text:
        return False
    return any(_summary_fragment_mentions_event(text, event) for event in events if isinstance(event, dict))


def _is_explicit_calendar_listing_fragment(fragment: str, events: list[dict]) -> bool:
    text = _normalize_optional_text(fragment)
    if not text or not _is_calendar_summary_fragment(text, events):
        return False
    return bool(_extract_date_from_text(text) or _extract_hhmm_from_text(text) or "—" in text)


def _join_macro_risk_components(components: list[str], *, calendar_events: list[dict] | None = None) -> str:
    cleaned = [_normalize_optional_text(component) for component in components if _normalize_optional_text(component)]
    if not cleaned:
        return ""

    calendar_events = calendar_events or []
    out = cleaned[0].rstrip(" ;")
    prev_is_explicit_calendar = _is_explicit_calendar_listing_fragment(cleaned[0], calendar_events)

    for component in cleaned[1:]:
        current = component.rstrip(" ;")
        current_is_explicit_calendar = _is_explicit_calendar_listing_fragment(current, calendar_events)
        if prev_is_explicit_calendar and current_is_explicit_calendar:
            separator = "; "
        elif out.endswith((".", "!", "?")):
            separator = " "
        else:
            separator = ". "
        out = f"{out}{separator}{current}"
        prev_is_explicit_calendar = current_is_explicit_calendar
    return out.strip()


def _compose_macro_risk_summary(
    raw_summary,
    *,
    structured_fragment="",
    calendar_events: list[dict] | None = None,
) -> str:
    events = [event for event in (calendar_events or []) if isinstance(event, dict)]
    raw_fragments = _collect_unique_text_fragments(raw_summary, max_fragments=6)
    structured_text = _normalize_optional_text(structured_fragment)

    if not structured_text:
        return _join_macro_risk_components(raw_fragments[:3], calendar_events=events)

    residual_fragments: list[str] = []
    for fragment in raw_fragments:
        if _semantic_texts_match(fragment, structured_text):
            continue
        if _is_calendar_summary_fragment(fragment, events):
            continue
        residual_fragments.append(fragment)

    calendar_summary = build_calendar_risk_summary(events) if events else ""
    components: list[str] = [structured_text]
    if calendar_summary:
        components.append(calendar_summary)
    if residual_fragments:
        components.extend(residual_fragments[: max(0, 3 - len(components))])

    return _join_macro_risk_components(components[:3], calendar_events=events)


def _harmonize_event_risk_warning_tokens(d: dict) -> None:
    warnings = d.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return

    legacy_wait_confirm = {
        "upcoming_high_impact_event_wait_confirm",
        "upcoming_high_impact_event_neutral_caution",
        "upcoming_high_impact_event_conservative_caution",
    }
    if any(item in legacy_wait_confirm for item in warnings):
        warnings = [item for item in warnings if item != "event_risk_wait_confirm_preferred"]

    if any(item in legacy_wait_confirm for item in warnings):
        warnings = [item for item in warnings if item != "event_risk_no_chasing"]

    d["warnings"] = warnings

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

def _in_night_low_liquidity_window_msk(mins: int) -> bool:
    # Night / low-liquidity window in MSK: 00:00–07:00 (end exclusive).
    return 0 <= int(mins) < 7 * 60


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
        # Aggressive must NOT be hard-blocked solely due to time windows / low-liquidity timing.
        # Tactic: force confirmation (wait_confirm) but keep trading allowed unless other core blockers apply.
        d["entry_mode"] = "wait_confirm"
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


_AGGRESSIVE_NIGHT_RISK_TEXT = (
    "⚠️ Низкая ликвидность (ночное окно): повышенный риск шпилек и ложных движений"
)


def apply_aggressive_night_low_liquidity_policy(d: dict) -> None:
    """
    Aggressive mode special-case for 00:00–07:00 MSK:
    - trading is allowed (no hard no_trade by time-of-day);
    - explicit risk warning is required;
    - entries must be confirmation-based (wait_confirm), especially for impulse/countertrend.
    """
    mode = normalize_mode(d.get("mode"))
    if mode != "aggressive":
        return
    if bool(d.get("no_trade")):
        return

    mins = _msk_minutes_from_time_str(d.get("time_msk"))
    if mins is None:
        mins = _msk_minutes_from_time_str(current_msk())
    if mins is None or not _in_night_low_liquidity_window_msk(mins):
        return

    d.setdefault("warnings", [])
    if isinstance(d.get("warnings"), list):
        _append_unique_str(d, "warnings", _AGGRESSIVE_NIGHT_RISK_TEXT)

    # At night we avoid instant/market entries; confirmation is mandatory.
    d["entry_mode"] = "wait_confirm"

    warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else []
    wl = " ".join(str(w or "").strip().lower() for w in warnings)
    if "impulse_no_exhale" in wl or "countertrend_aggressive" in wl:
        # Ensure the signal remains actionable: prefer explicit confirmation checklist.
        if not (d.get("confirmation_rules") or ""):
            d["confirmation_rules"] = (
                "Ночное окно: вход только после подтверждения (1–2 свечи удержания), "
                "без нового экстремума и с нормализацией объёма."
            )


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
        ctx = dict(raw)
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

    ctx_events, ctx_summary = _normalize_macro_event_bundle(
        ctx.get("upcoming_events"),
        ctx.get("macro_risk_summary"),
    )
    ctx["upcoming_events"] = _merge_upcoming_event_lists(ctx_events)
    ctx["macro_risk_summary"] = _merge_unique_texts(ctx_summary, max_fragments=3)
    event_risk_snapshot = normalize_event_risk_snapshot(
        {
            "timestamp_utc": ctx.get("event_risk_context_timestamp_utc"),
            "event_risk_context": ctx.get("event_risk_context"),
        }
    )
    ctx["event_risk_context"] = event_risk_snapshot.get("event_risk_context") or []
    ctx["event_risk_context_timestamp_utc"] = event_risk_snapshot.get("timestamp_utc")

    d["day_mid_context"] = ctx


def _normalize_report_macro_context(report_payload) -> dict | None:
    if not isinstance(report_payload, dict):
        return None

    out = dict(report_payload)
    events, summary = _normalize_macro_event_bundle(
        out.get("upcoming_events"),
        out.get("macro_risk_summary"),
    )
    out["upcoming_events"] = _merge_upcoming_event_lists(events)
    out["macro_risk_summary"] = _merge_unique_texts(summary, max_fragments=3)
    event_risk_snapshot = normalize_event_risk_snapshot(
        {
            "timestamp_utc": out.get("event_risk_context_timestamp_utc"),
            "event_risk_context": out.get("event_risk_context"),
        }
    )
    out["event_risk_context"] = event_risk_snapshot.get("event_risk_context") or []
    out["event_risk_context_timestamp_utc"] = event_risk_snapshot.get("timestamp_utc")

    tmp = {"day_mid_context": out.get("day_mid_context")}
    _normalize_day_mid_context(tmp)
    out["day_mid_context"] = tmp.get("day_mid_context") or {
        "day_bias": None,
        "mid_bias": None,
        "notes": None,
        "upcoming_events": [],
        "macro_risk_summary": "",
        "event_risk_context": [],
        "event_risk_context_timestamp_utc": event_risk_snapshot.get("timestamp_utc"),
    }
    return out


def merge_day_mid_report_context(
    d: dict,
    *,
    day_report: dict | None = None,
    mid_report: dict | None = None,
) -> None:
    if not isinstance(d, dict):
        return

    ensure_macro_event_fields(d)
    _normalize_day_mid_context(d)
    now_dt = _resolve_macro_event_now_dt(d.get("time_msk"))

    day_ctx = _normalize_report_macro_context(day_report)
    mid_ctx = _normalize_report_macro_context(mid_report)
    ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
    ctx = dict(ctx)

    if now_dt is not None:
        _apply_relevant_macro_event_filter(d, now_dt=now_dt)
        _apply_relevant_macro_event_filter(ctx, now_dt=now_dt)
        _apply_relevant_macro_event_filter(day_ctx, now_dt=now_dt)
        _apply_relevant_macro_event_filter(mid_ctx, now_dt=now_dt)

    for report_ctx in (day_ctx, mid_ctx):
        if not isinstance(report_ctx, dict):
            continue
        report_dm = report_ctx.get("day_mid_context") if isinstance(report_ctx.get("day_mid_context"), dict) else {}
        if ctx.get("day_bias") is None and report_dm.get("day_bias") is not None:
            ctx["day_bias"] = report_dm.get("day_bias")
        if ctx.get("mid_bias") is None and report_dm.get("mid_bias") is not None:
            ctx["mid_bias"] = report_dm.get("mid_bias")

    ctx["upcoming_events"] = _merge_upcoming_event_lists(
        (day_ctx or {}).get("upcoming_events"),
        (mid_ctx or {}).get("upcoming_events"),
        ctx.get("upcoming_events"),
    )
    ctx["macro_risk_summary"] = _merge_unique_texts(
        (day_ctx or {}).get("macro_risk_summary"),
        (mid_ctx or {}).get("macro_risk_summary"),
        ctx.get("macro_risk_summary"),
    )

    merged_notes = _merge_unique_texts(
        ((day_ctx or {}).get("day_mid_context") or {}).get("notes"),
        ((mid_ctx or {}).get("day_mid_context") or {}).get("notes"),
        ctx.get("notes"),
    )
    ctx["notes"] = merged_notes or None
    event_risk_snapshot = merge_event_risk_snapshots(
        {
            "timestamp_utc": ((day_ctx or {}).get("day_mid_context") or {}).get("event_risk_context_timestamp_utc"),
            "event_risk_context": ((day_ctx or {}).get("day_mid_context") or {}).get("event_risk_context"),
        },
        {
            "timestamp_utc": ((mid_ctx or {}).get("day_mid_context") or {}).get("event_risk_context_timestamp_utc"),
            "event_risk_context": ((mid_ctx or {}).get("day_mid_context") or {}).get("event_risk_context"),
        },
        {
            "timestamp_utc": (day_ctx or {}).get("event_risk_context_timestamp_utc"),
            "event_risk_context": (day_ctx or {}).get("event_risk_context"),
        },
        {
            "timestamp_utc": (mid_ctx or {}).get("event_risk_context_timestamp_utc"),
            "event_risk_context": (mid_ctx or {}).get("event_risk_context"),
        },
        {
            "timestamp_utc": ctx.get("event_risk_context_timestamp_utc"),
            "event_risk_context": ctx.get("event_risk_context"),
        },
        {
            "timestamp_utc": d.get("event_risk_context_timestamp_utc"),
            "event_risk_context": d.get("event_risk_context"),
        },
    )
    ctx["event_risk_context"] = copy.deepcopy(event_risk_snapshot.get("event_risk_context") or [])
    ctx["event_risk_context_timestamp_utc"] = event_risk_snapshot.get("timestamp_utc")

    if isinstance(day_ctx, dict):
        if day_ctx.get("time_msk"):
            ctx["day_report_time_msk"] = day_ctx.get("time_msk")
    if isinstance(mid_ctx, dict):
        if mid_ctx.get("time_msk"):
            ctx["mid_report_time_msk"] = mid_ctx.get("time_msk")

    d["day_mid_context"] = ctx
    d["upcoming_events"] = _merge_upcoming_event_lists(ctx.get("upcoming_events"), d.get("upcoming_events"))
    d["macro_risk_summary"] = _merge_unique_texts(ctx.get("macro_risk_summary"), d.get("macro_risk_summary"))
    d["event_risk_context"] = copy.deepcopy(event_risk_snapshot.get("event_risk_context") or [])
    d["event_risk_context_timestamp_utc"] = event_risk_snapshot.get("timestamp_utc")


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


def _parse_msk_date(value) -> datetime | None:
    text = _normalize_optional_text(value)
    if not text:
        return None
    cleaned = re.sub(r"\b(MSK|МСК)\b", "", text, flags=re.IGNORECASE).strip()
    tz = ZoneInfo("Europe/Moscow")
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=tz)
        except Exception:
            pass
    return None


def _parse_msk_datetime(value, *, date_hint=None, fallback_dt: datetime | None = None) -> datetime | None:
    text = _normalize_optional_text(value)
    if not text:
        return None
    if re.search(r"\btbd\b", text, flags=re.IGNORECASE):
        return None

    cleaned = re.sub(r"\b(MSK|МСК)\b", "", text, flags=re.IGNORECASE).strip()
    tz = ZoneInfo("Europe/Moscow")

    for fmt in ("%d.%m.%Y, %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y, %H:%M", "%d.%m.%y %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=tz)
        except Exception:
            pass

    m = re.search(r"(\d{1,2}):(\d{2})", cleaned)
    if not m:
        return None

    base_dt = _parse_msk_date(date_hint) if date_hint else None
    if base_dt is None:
        base_dt = fallback_dt
    if base_dt is None:
        base_dt = _parse_msk_datetime(current_msk())
    if base_dt is None:
        return None

    try:
        hh = int(m.group(1))
        mm = int(m.group(2))
    except Exception:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return base_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _normalize_event_impact(value) -> str:
    text = _normalize_optional_text(value).lower().replace("-", "_").replace(" ", "_")
    if text in {"critical", "very_high", "veryhigh", "severe"}:
        return "high"
    if text in {"med", "moderate"}:
        return "medium"
    if text in {"minor"}:
        return "low"
    return text


def _event_time_is_tbd(event: dict) -> bool:
    time_text = _normalize_optional_text(event.get("time_msk"))
    return (not time_text) or bool(re.search(r"\btbd\b", time_text, flags=re.IGNORECASE))


def _event_window_minutes(event: dict) -> tuple[int, int]:
    impact = _normalize_event_impact(event.get("impact"))
    before = _to_float(event.get("window_before_min"))
    after = _to_float(event.get("window_after_min"))
    if before is None:
        before = 90.0 if impact == "high" else (45.0 if impact == "medium" else 30.0)
    if after is None:
        after = 60.0 if impact == "high" else (30.0 if impact == "medium" else 15.0)
    return max(int(before), 0), max(int(after), 0)


def _event_display_time(event: dict) -> str:
    time_text = _normalize_optional_text(event.get("time_msk"))
    date_text = _normalize_optional_text(event.get("date_msk"))
    if _event_time_is_tbd(event):
        if date_text:
            return f"{date_text}, TBD"
        return "TBD"
    if time_text and re.search(r"\d{2}\.\d{2}\.\d{4}", time_text):
        return time_text
    if time_text and date_text:
        return f"{date_text}, {time_text}"
    return time_text or date_text or "TBD"


def _resolve_macro_event_now_dt(now_msk=None) -> datetime | None:
    now_dt = _parse_msk_datetime(now_msk) if now_msk is not None else None
    if now_dt is None:
        now_dt = _parse_msk_datetime(current_msk())
    return now_dt


def _event_reference_date(event: dict) -> datetime | None:
    if not isinstance(event, dict):
        return None
    date_text = _normalize_optional_text(event.get("date_msk")) or _extract_date_from_text(event.get("time_msk"))
    return _parse_msk_date(date_text)


def _event_is_relevant_now(event: dict, *, now_dt: datetime) -> bool:
    if not isinstance(event, dict):
        return False

    time_text = _normalize_optional_text(event.get("time_msk"))
    if _event_time_is_tbd(event):
        date_dt = _event_reference_date(event)
        if re.search(r"\btbd\b", time_text, flags=re.IGNORECASE):
            return date_dt is not None and date_dt.date() >= now_dt.date()
        if date_dt is not None:
            return date_dt.date() >= now_dt.date()
        return True

    event_dt = _parse_msk_datetime(
        event.get("time_msk"),
        date_hint=event.get("date_msk"),
        fallback_dt=now_dt,
    )
    if event_dt is None:
        date_dt = _event_reference_date(event)
        return date_dt is not None and date_dt.date() >= now_dt.date()

    _, after_min = _event_window_minutes(event)
    return event_dt >= now_dt or (event_dt + timedelta(minutes=after_min)) >= now_dt


def _filter_relevant_upcoming_events(raw_events, *, now_dt: datetime) -> list[dict]:
    filtered: list[dict] = []
    for event in _merge_upcoming_event_lists(raw_events):
        if _event_is_relevant_now(event, now_dt=now_dt):
            filtered.append(event)
    return filtered


def _sanitize_macro_event_bundle(raw_events, raw_summary, *, now_dt: datetime) -> tuple[list[dict], str]:
    normalized_events, normalized_summary = _normalize_macro_event_bundle(raw_events, raw_summary)
    merged_events = _merge_upcoming_event_lists(normalized_events)
    filtered_events = _filter_relevant_upcoming_events(merged_events, now_dt=now_dt)
    summary = _merge_unique_texts(normalized_summary, max_fragments=3)

    if merged_events and len(filtered_events) != len(merged_events):
        summary = build_calendar_risk_summary(filtered_events) if filtered_events else ""

    return filtered_events, summary


def _apply_relevant_macro_event_filter(container: dict | None, *, now_dt: datetime) -> None:
    if not isinstance(container, dict):
        return
    events, summary = _sanitize_macro_event_bundle(
        container.get("upcoming_events"),
        container.get("macro_risk_summary"),
        now_dt=now_dt,
    )
    container["upcoming_events"] = events
    container["macro_risk_summary"] = summary


def _event_label(event: dict) -> str:
    name = _normalize_optional_text(event.get("event")) or "Upcoming event"
    return f"{name} ({_event_display_time(event)} МСК)"


def _calendar_empty_message_for_event_risk(snapshot) -> str:
    default = "Календарь пуст: подтвержденных scheduled events сейчас нет."
    normalized = normalize_event_risk_snapshot(snapshot)
    items = normalized.get("event_risk_context") if isinstance(normalized, dict) else []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if _normalize_optional_text(item.get("category")).lower() != "geopolitics":
            continue
        if _normalize_optional_text(item.get("phase")).lower() not in {"pre_event", "ongoing"}:
            continue
        blob = " ".join(
            [
                _normalize_optional_text(item.get("event")),
                _normalize_optional_text(item.get("source_title")),
                " ".join(str(x) for x in (item.get("confirmed_facts") or []) if isinstance(x, str)),
                " ".join(str(x) for x in (item.get("anticipated_consequences") or []) if isinstance(x, str)),
            ]
        ).lower()
        if any(
            marker in blob
            for marker in (
                "white house",
                "oval office",
                "press conference",
                "briefing",
                "remarks",
                "talks",
                "meeting",
                "ceasefire",
                "truce",
                "deadline",
            )
        ):
            label = _normalize_optional_text(item.get("event")) or "geopolitical headline event"
            return (
                "Календарь scheduled events пуст, но active scheduled-adjacent geopolitical headline risk: "
                f"{label}."
            )
    regime_layer = normalized.get("regime_layer") if isinstance(normalized.get("regime_layer"), dict) else {}
    if (
        _normalize_optional_text(regime_layer.get("driver")).lower() == "geopolitics"
        and _normalize_optional_text(regime_layer.get("severity")).lower() in {"high", "severe"}
    ):
        return "Календарь scheduled events пуст, но active unscheduled geopolitical headline risk остаётся в силе."
    return default


def _downgrade_confidence(d: dict, steps: int = 1) -> None:
    order = ("Low", "Medium", "High")
    current = _normalize_optional_text(d.get("confidence"))
    if current not in order:
        current = "Medium"
    idx = order.index(current)
    idx = max(idx - max(int(steps), 0), 0)
    d["confidence"] = order[idx]


def _upgrade_confidence(d: dict, steps: int = 1) -> None:
    order = ("Low", "Medium", "High")
    current = _normalize_optional_text(d.get("confidence"))
    if current not in order:
        current = "Medium"
    idx = order.index(current)
    idx = min(idx + max(int(steps), 0), len(order) - 1)
    d["confidence"] = order[idx]


def _event_risk_setup_is_marginal(d: dict, *, mode: str) -> bool:
    confidence = _normalize_optional_text(d.get("confidence"))
    if confidence in {"Low", "Medium"}:
        return True

    entry_mode = str(d.get("entry_mode") or "").strip().lower()
    if entry_mode in {"now", "market", "wait_confirm"}:
        return True

    warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else []
    wl = " ".join(str(w or "").strip().lower() for w in warnings)
    risk_needles = (
        "impulse_no_exhale",
        "phase_between",
        "ema_between_m15_h1",
        "neutral_wait_confirm_due_to_volatility",
        "neutral_wait_confirm_due_to_rr",
        "dir_guard_forced",
        "countertrend",
    )
    if any(needle in wl for needle in risk_needles):
        return True

    rr_by_mode = d.get("rr_by_mode") if isinstance(d.get("rr_by_mode"), dict) else {}
    rr_val = _to_float(rr_by_mode.get(mode))
    if rr_val is None:
        rr_val = _to_float(d.get("rr"))
    return rr_val is None or rr_val < 1.8


def _ensure_event_wait_confirm_rules(d: dict, event_label: str) -> None:
    text = (
        f"Дождаться реакции после события {event_label}: без резкого спайка/расширения спреда, "
        "затем подтверждение удержания уровня."
    )
    raw = d.get("confirmation_rules")
    if isinstance(raw, str):
        if event_label not in raw:
            d["confirmation_rules"] = (raw.strip() + " " + text).strip()
        return
    if isinstance(raw, list):
        joined = " ".join(str(x).strip() for x in raw if str(x).strip())
        if event_label not in joined:
            raw.append(text)
        return
    d["confirmation_rules"] = text


def _ensure_structured_event_confirmation_rules(
    d: dict,
    *,
    event_label: str,
    phase: str,
    volatility_risk: str,
) -> None:
    label = event_label.strip() or "headline catalyst"
    phase_key = _normalize_optional_text(phase).lower()
    risk_key = _normalize_optional_text(volatility_risk).lower()
    if phase_key in {"pre_event", "ongoing"}:
        text = (
            f"{label}: вход только после подтверждения структуры/ретеста; "
            "не догонять первый импульс и ждать удержания уровня."
        )
    elif phase_key == "post_event" and risk_key == "high":
        text = (
            f"{label}: после события ждать стабилизации follow-through и ретеста; "
            "ложные пробои и нестабильные продолжения вероятны."
        )
    else:
        text = (
            f"{label}: вход только после подтверждения структуры; "
            "исполнение без подтверждения не допускается."
        )

    raw = d.get("confirmation_rules")
    if isinstance(raw, str):
        if text not in raw:
            d["confirmation_rules"] = (raw.strip() + " " + text).strip()
        return
    if isinstance(raw, list):
        joined = " ".join(str(x).strip() for x in raw if str(x).strip())
        if text not in joined:
            raw.append(text)
        return
    d["confirmation_rules"] = text


def _event_risk_level_rank(value) -> int:
    text = _normalize_optional_text(value).lower()
    return {
        "none": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
    }.get(text, 0)


def _max_event_risk_level(*values) -> str:
    best = "none"
    best_rank = -1
    for value in values:
        rank = _event_risk_level_rank(value)
        if rank > best_rank:
            best = _normalize_optional_text(value).lower() or "none"
            best_rank = rank
    return best if best in {"none", "low", "medium", "high"} else "none"


def _event_risk_regime_rank(value) -> int:
    text = _normalize_optional_text(value).lower()
    return {
        "": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
        "severe": 4,
    }.get(text, 0)


def _structured_event_regime_layer(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    layer = snapshot.get("regime_layer") if isinstance(snapshot.get("regime_layer"), dict) else {}
    if not layer:
        return {}
    return {
        "driver": _normalize_optional_text(layer.get("driver")).lower(),
        "severity": _normalize_optional_text(layer.get("severity")).lower(),
        "flags": list(layer.get("flags") or []) if isinstance(layer.get("flags"), list) else [],
        "dominant_event": _normalize_optional_text(layer.get("dominant_event")),
        "summary": _normalize_optional_text(layer.get("summary")),
        "risk_asymmetry": _normalize_optional_text(layer.get("risk_asymmetry")).lower(),
        "continuation_mode": _normalize_optional_text(layer.get("continuation_mode")).lower(),
        "strictness": _normalize_optional_text(layer.get("strictness")).lower(),
    }


def _signal_event_driver_from_category(value) -> str:
    category = _normalize_optional_text(value).lower()
    if category == "geopolitics":
        return "geopolitics"
    if category == "crypto_market_structure":
        return "crypto_structure"
    if category:
        return "macro"
    return "none"


def _merged_signal_event_risk_snapshot(d: dict) -> dict:
    ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
    return merge_event_risk_snapshots(
        {
            "timestamp_utc": ctx.get("event_risk_context_timestamp_utc"),
            "event_risk_context": ctx.get("event_risk_context"),
        },
        {
            "timestamp_utc": d.get("event_risk_context_timestamp_utc"),
            "event_risk_context": d.get("event_risk_context"),
        },
    )


def _structured_event_text_blob(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return " ".join(
        [
            _normalize_optional_text(item.get("event")),
            _normalize_optional_text(item.get("summary")),
            " ".join(item.get("drivers") or []),
            " ".join(item.get("confirmed_facts") or []),
            " ".join(item.get("anticipated_consequences") or []),
            " ".join(item.get("realized_market_events") or []),
            " ".join(item.get("recent_developments") or []),
        ]
    ).lower()


def _structured_event_volatility_risk(item: dict) -> str:
    if not isinstance(item, dict):
        return "low"
    driver = _signal_event_driver_from_category(item.get("category"))
    impact = _normalize_event_impact(item.get("impact")) or "none"
    phase = _normalize_optional_text(item.get("phase")).lower() or "none"
    blob = _structured_event_text_blob(item)
    high_vol_needles = (
        "headline sensitivity",
        "volatility",
        "risk premium",
        "liquidation",
        "false break",
        "unstable",
        "fragile",
        "shipping",
        "blockade",
        "sanctions",
        "tariff",
        "attack",
        "strike",
        "outage",
    )

    if impact == "high":
        if phase in {"pre_event", "ongoing"}:
            return "high"
        if phase == "post_event":
            if driver == "geopolitics" or any(needle in blob for needle in high_vol_needles):
                return "high"
            return "medium"
    if impact == "medium":
        if phase in {"pre_event", "ongoing"}:
            return "medium"
        if phase == "post_event" and any(needle in blob for needle in high_vol_needles):
            return "medium"
    return "low"


def _build_structured_macro_risk_fragment(
    item: dict,
    *,
    driver: str,
    volatility_risk: str,
    regime_layer: dict | None = None,
    calendar_empty: bool = False,
) -> str:
    if not isinstance(item, dict) or driver == "none":
        return ""

    phase = _normalize_optional_text(item.get("phase")).lower() or "none"
    regime_layer = regime_layer if isinstance(regime_layer, dict) else {}
    regime_driver = _normalize_optional_text(regime_layer.get("driver")).lower()
    regime_severity = _normalize_optional_text(regime_layer.get("severity")).lower()
    if regime_driver == "geopolitics" and regime_severity == "severe":
        text = (
            "тяжёлый геополитический режим остаётся нерешённым; фон чувствителен к эскалации и "
            "сохраняет асимметричный риск резкого downside-движения."
        )
        if calendar_empty:
            text += " Пустой scheduled calendar не снижает уязвимость к внеплановым заголовкам."
        return text

    if driver == "geopolitics":
        if phase in {"pre_event", "ongoing"}:
            return "геополитическая траектория эскалации остаётся нерешённой; рынок по-прежнему живёт заголовками."
        if phase == "post_event" and volatility_risk == "high":
            return "последствия геополитического шока остаются нестабильными; follow-through всё ещё задаётся заголовками."
        return "геополитический риск остаётся живым execution-фактором."

    if driver == "macro":
        if phase in {"pre_event", "ongoing"}:
            return "траектория макрокатализатора остаётся нерешённой; волатильность первого движения может вводить в заблуждение."
        if phase == "post_event" and volatility_risk in {"medium", "high"}:
            return "после события макропереоценка остаётся нестабильной; follow-through требует подтверждения."
        return "макрориск по-прежнему важен для исполнения."

    if driver == "crypto_structure":
        if phase in {"pre_event", "ongoing"}:
            return "стресс в crypto-structure остаётся нерешённым; возможны ложные пробои и движения на ликвидациях."
        if phase == "post_event" and volatility_risk in {"medium", "high"}:
            return "после катализатора stress в crypto-structure остаётся повышенным; follow-through всё ещё нестабилен."
        return "стресс в crypto-structure остаётся актуальным риском исполнения."

    return _merge_unique_texts(item.get("summary"), max_fragments=1)


def _derive_signal_event_risk_summary(
    d: dict,
    *,
    merged_events: list[dict],
    merged_summary: str,
    now_dt: datetime,
) -> dict:
    snapshot = _merged_signal_event_risk_snapshot(d)
    items = snapshot.get("event_risk_context") or []
    dominant_item = items[0] if items else None
    regime_layer = _structured_event_regime_layer(snapshot)

    driver = _signal_event_driver_from_category((dominant_item or {}).get("category"))
    impact = _normalize_event_impact((dominant_item or {}).get("impact")) or "none"
    if impact not in {"high", "medium", "low"}:
        impact = "none"
    phase = _normalize_optional_text((dominant_item or {}).get("phase")).lower() or "none"
    if phase not in {"pre_event", "ongoing", "post_event"}:
        phase = "none"

    volatility_risk = _structured_event_volatility_risk(dominant_item)
    regime_driver = _normalize_optional_text(regime_layer.get("driver")).lower()
    regime_severity = _normalize_optional_text(regime_layer.get("severity")).lower()
    severe_geopolitical_regime = regime_driver == "geopolitics" and regime_severity == "severe"

    active_high = False
    active_other = False
    tbd_high = False
    for event in merged_events:
        event_impact = _normalize_event_impact(event.get("impact"))
        if _event_time_is_tbd(event):
            if event_impact == "high":
                tbd_high = True
            continue
        event_dt = _parse_msk_datetime(
            event.get("time_msk"),
            date_hint=event.get("date_msk"),
            fallback_dt=now_dt,
        )
        if event_dt is None:
            continue
        before_min, after_min = _event_window_minutes(event)
        is_active = (event_dt - timedelta(minutes=before_min)) <= now_dt <= (event_dt + timedelta(minutes=after_min))
        if not is_active:
            continue
        if event_impact == "high":
            active_high = True
        else:
            active_other = True

    execution_caution = "low"
    unresolved_high_impact = bool(driver != "none" and impact == "high" and phase in {"pre_event", "ongoing"})
    post_event_unstable = bool(driver != "none" and phase == "post_event" and volatility_risk == "high")
    calendar_empty = not bool(merged_events)

    if driver == "geopolitics" and impact == "high":
        execution_caution = "high"
    elif unresolved_high_impact:
        execution_caution = "high"
    elif post_event_unstable:
        execution_caution = "high"
    elif driver != "none" and (impact in {"high", "medium"} or volatility_risk == "medium"):
        execution_caution = "medium"

    if severe_geopolitical_regime:
        volatility_risk = _max_event_risk_level(volatility_risk, "high")
        execution_caution = _max_event_risk_level(execution_caution, "high")
        unresolved_high_impact = True

    if active_high:
        volatility_risk = _max_event_risk_level(volatility_risk, "high")
        execution_caution = _max_event_risk_level(execution_caution, "high")
    elif active_other or tbd_high:
        volatility_risk = _max_event_risk_level(volatility_risk, "medium")
        execution_caution = _max_event_risk_level(execution_caution, "medium")

    macro_fragment = _build_structured_macro_risk_fragment(
        dominant_item,
        driver=driver,
        volatility_risk=volatility_risk,
        regime_layer=regime_layer,
        calendar_empty=calendar_empty,
    )

    execution_line = ""
    if severe_geopolitical_regime:
        execution_line = (
            "тяжёлый геополитический режим: continuation допустим только тактически; нужен ретест/подтверждение, "
            "invalidation должен быть явным, без небрежного buy-the-dip."
        )
    elif post_event_unstable:
        execution_line = "послесобытийная волатильность остаётся нестабильной; не преследуй ложные пробои."
    elif driver == "geopolitics" and impact == "high":
        execution_line = "волатильность на заголовках повышена; нужен confirm, без погони за первым движением."
    elif unresolved_high_impact:
        execution_line = "риск по нерешённому катализатору высок; подтверждение важнее первого импульса."
    elif execution_caution == "medium" and driver != "none":
        execution_line = "чувствительность к заголовкам повышена; предпочтителен confirm."

    warning_tokens: list[str] = []
    if driver == "geopolitics" and impact == "high":
        warning_tokens.append("event_risk_geopolitical_execution_caution")
    if severe_geopolitical_regime:
        warning_tokens.extend(
            [
                "event_risk_geopolitical_regime_severe",
                "event_risk_downside_shock_asymmetry",
                "event_risk_tactical_only_continuation",
            ]
        )
    if unresolved_high_impact:
        warning_tokens.append("event_risk_unresolved_high_impact_catalyst")
    if post_event_unstable:
        warning_tokens.append("event_risk_post_event_unstable_follow_through")
    if execution_caution in {"medium", "high"}:
        warning_tokens.append("event_risk_no_chasing")
    if unresolved_high_impact or post_event_unstable:
        warning_tokens.append("event_risk_continuation_stricter")

    return {
        "summary": {
            "dominant_driver": driver,
            "max_impact": impact,
            "dominant_phase": phase,
            "volatility_risk": volatility_risk if volatility_risk in {"high", "medium", "low"} else "low",
            "execution_caution": execution_caution if execution_caution in {"high", "medium", "low"} else "low",
        },
        "dominant_item": copy.deepcopy(dominant_item) if isinstance(dominant_item, dict) else None,
        "macro_fragment": macro_fragment,
        "execution_line": execution_line,
        "warning_tokens": warning_tokens,
        "prefer_wait_confirm": bool(
            unresolved_high_impact
            or (driver == "geopolitics" and impact == "high")
            or severe_geopolitical_regime
        ),
        "confidence_steps": (
            2
            if severe_geopolitical_regime
            else 1
            if (driver != "none" and (impact in {"high", "medium"} or execution_caution in {"medium", "high"}))
            else 0
        ),
        "regime_layer": copy.deepcopy(regime_layer) if regime_layer else {},
        "snapshot": snapshot,
        "has_structured_risk": bool(driver != "none"),
    }


def apply_upcoming_event_risk(d: dict) -> None:
    if not isinstance(d, dict):
        return

    ensure_warnings_list(d)
    ensure_macro_event_fields(d)
    _normalize_day_mid_context(d)
    now_dt = _resolve_macro_event_now_dt(d.get("time_msk"))
    if now_dt is None:
        return

    ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
    own_events, own_summary = _sanitize_macro_event_bundle(
        d.get("upcoming_events"),
        d.get("macro_risk_summary"),
        now_dt=now_dt,
    )
    ctx_events, ctx_summary = _sanitize_macro_event_bundle(
        ctx.get("upcoming_events"),
        ctx.get("macro_risk_summary"),
        now_dt=now_dt,
    )
    merged_events = _merge_upcoming_event_lists(ctx_events, own_events)
    merged_summary = _merge_unique_texts(ctx_summary, own_summary)

    structured_state = _derive_signal_event_risk_summary(
        d,
        merged_events=merged_events,
        merged_summary=merged_summary,
        now_dt=now_dt,
    )
    event_risk_summary = structured_state.get("summary") or {
        "dominant_driver": "none",
        "max_impact": "none",
        "dominant_phase": "none",
        "volatility_risk": "low",
        "execution_caution": "low",
    }

    merged_summary = _compose_macro_risk_summary(
        merged_summary,
        structured_fragment=structured_state.get("macro_fragment"),
        calendar_events=merged_events,
    )
    d["upcoming_events"] = merged_events
    d["macro_risk_summary"] = merged_summary
    d["event_risk_summary"] = copy.deepcopy(event_risk_summary)
    regime_layer = structured_state.get("regime_layer") if isinstance(structured_state.get("regime_layer"), dict) else {}
    if regime_layer:
        d["event_risk_regime"] = copy.deepcopy(regime_layer)
    else:
        d.pop("event_risk_regime", None)
    if isinstance(ctx, dict):
        ctx["upcoming_events"] = copy.deepcopy(merged_events)
        ctx["macro_risk_summary"] = merged_summary
        d["day_mid_context"] = ctx

    if not merged_events and not merged_summary and event_risk_summary.get("dominant_driver") == "none":
        return

    active_high: list[dict] = []
    active_other: list[dict] = []
    tbd_events: list[dict] = []

    for event in merged_events:
        impact = _normalize_event_impact(event.get("impact"))
        if _event_time_is_tbd(event):
            tbd_events.append(
                {
                    "event": _normalize_optional_text(event.get("event")) or "Upcoming event",
                    "time_msk": _event_display_time(event),
                    "impact": impact or "",
                }
            )
            continue

        event_dt = _parse_msk_datetime(
            event.get("time_msk"),
            date_hint=event.get("date_msk"),
            fallback_dt=now_dt,
        )
        if event_dt is None:
            tbd_events.append(
                {
                    "event": _normalize_optional_text(event.get("event")) or "Upcoming event",
                    "time_msk": _event_display_time(event),
                    "impact": impact or "",
                }
            )
            continue

        before_min, after_min = _event_window_minutes(event)
        if (event_dt - timedelta(minutes=before_min)) <= now_dt <= (event_dt + timedelta(minutes=after_min)):
            bucket = {
                "event": _normalize_optional_text(event.get("event")) or "Upcoming event",
                "time_msk": _event_display_time(event),
                "impact": impact or "",
            }
            if impact == "high":
                active_high.append(bucket)
            else:
                active_other.append(bucket)

    mode = normalize_mode(d.get("mode"))
    event_risk = {
        "active_events": active_high + active_other,
        "tbd_events": tbd_events,
        "macro_risk_summary": merged_summary,
        "mode_action": "none",
        "display_lines": [],
        "signal_summary": copy.deepcopy(event_risk_summary),
    }

    if active_other:
        _append_unique_str(d, "warnings", "upcoming_event_caution_window")
        _downgrade_confidence(d, 1)
        if mode in {"aggressive", "neutral"} and not bool(d.get("no_trade")):
            d["entry_mode"] = "wait_confirm"

    if active_high:
        primary = active_high[0]
        label = f"{primary['event']} ({primary['time_msk']} МСК)"
        marginal_setup = _event_risk_setup_is_marginal(d, mode=mode)
        if mode == "aggressive":
            d["entry_mode"] = "wait_confirm"
            _append_unique_str(d, "warnings", "upcoming_high_impact_event_wait_confirm")
            _downgrade_confidence(d, 1)
            _ensure_event_wait_confirm_rules(d, label)
            event_risk["mode_action"] = "wait_confirm"
            event_risk["display_lines"].append(
                f"⚠️ Event risk: {label} — активное окно high-impact события; aggressive переведён в wait_confirm."
            )
        elif mode == "neutral":
            d["entry_mode"] = "wait_confirm"
            _append_unique_str(d, "warnings", "upcoming_high_impact_event_neutral_caution")
            if marginal_setup:
                _append_unique_str(d, "warnings", "upcoming_high_impact_event_marginal_setup_strict")
            _ensure_event_wait_confirm_rules(d, label)
            event_risk["mode_action"] = "wait_confirm"
            if marginal_setup:
                event_risk["display_lines"].append(
                    f"⚠️ Event risk: {label} — активное окно high-impact события; marginal neutral setup не блокируется, но требует подтверждения/ретеста без погони за первым импульсом."
                )
            else:
                event_risk["display_lines"].append(
                    f"⚠️ Event risk: {label} — активное окно high-impact события; neutral требует wait_confirm."
                )
            _downgrade_confidence(d, 1)
        elif mode == "conservative":
            d["entry_mode"] = "wait_confirm"
            _append_unique_str(d, "warnings", "upcoming_high_impact_event_conservative_caution")
            if marginal_setup:
                _append_unique_str(d, "warnings", "upcoming_high_impact_event_marginal_setup_strict")
            _ensure_event_wait_confirm_rules(d, label)
            _downgrade_confidence(d, 1)
            event_risk["mode_action"] = "wait_confirm"
            if marginal_setup:
                event_risk["display_lines"].append(
                    f"⚠️ Event risk: {label} — активное окно high-impact события; conservative требует подтверждения/ретеста и не допускает погоню за первым движением."
                )
            else:
                event_risk["display_lines"].append(
                    f"⚠️ Event risk: {label} — активное окно high-impact события; conservative требует wait_confirm."
                )

    if (tbd_events or (merged_summary and merged_events)) and not active_high:
        _append_unique_str(d, "warnings", "upcoming_event_tbd_macro_caution")
        _downgrade_confidence(d, 1)
        if tbd_events:
            primary_tbd = tbd_events[0]
            event_risk["display_lines"].append(
                f"⚠️ Макро/геориск: {primary_tbd['event']} ({primary_tbd['time_msk']} МСК) — точное время не задано, жёсткой блокировки нет; уверенность снижена."
            )
        elif merged_summary:
            event_risk["display_lines"].append(
                "⚠️ Макро/геориск: ближайшее окно риска без точного времени; жёсткой блокировки нет, но уверенность снижена."
            )
        if event_risk["mode_action"] == "none":
            event_risk["mode_action"] = "confidence_down"

    if structured_state.get("has_structured_risk"):
        dominant_item = structured_state.get("dominant_item") if isinstance(structured_state.get("dominant_item"), dict) else {}
        dominant_event_label = _normalize_optional_text(dominant_item.get("event")) or "headline catalyst"
        dominant_phase = _normalize_optional_text(event_risk_summary.get("dominant_phase")).lower()
        volatility_risk = _normalize_optional_text(event_risk_summary.get("volatility_risk")).lower()
        if structured_state.get("prefer_wait_confirm") and mode in {"aggressive", "neutral", "conservative"} and not bool(d.get("no_trade")):
            d["entry_mode"] = "wait_confirm"
            if event_risk["mode_action"] in {"none", "confidence_down"}:
                event_risk["mode_action"] = "wait_confirm"
            _append_unique_str(d, "warnings", "event_risk_wait_confirm_preferred")
            _ensure_structured_event_confirmation_rules(
                d,
                event_label=dominant_event_label,
                phase=dominant_phase,
                volatility_risk=volatility_risk,
            )
        if structured_state.get("confidence_steps"):
            _downgrade_confidence(d, int(structured_state.get("confidence_steps") or 0))
        for warning in structured_state.get("warning_tokens") or []:
            _append_unique_str(d, "warnings", warning)
        _harmonize_event_risk_warning_tokens(d)

        structured_lines: list[str] = []
        macro_fragment = _normalize_optional_text(structured_state.get("macro_fragment"))
        if macro_fragment:
            structured_lines.append(f"⚠️ Макро/геориск: {macro_fragment}")
        execution_line = _normalize_optional_text(structured_state.get("execution_line"))
        if execution_line:
            structured_lines.append(f"⚠️ Риск исполнения: {execution_line}")
        if structured_lines:
            event_risk["display_lines"] = [*structured_lines, *(event_risk.get("display_lines") or [])]

    has_event_risk_factor = bool(
        active_high
        or active_other
        or tbd_events
        or structured_state.get("has_structured_risk")
    )
    if bool(d.get("no_trade")) and has_event_risk_factor:
        secondary_line = (
            "⚠️ Событийный риск: повышенный риск по катализаторам здесь вторичен; "
            "основная причина отказа от сделки остаётся структурной."
        )
        existing_lines_low = " ".join(str(x or "").strip().lower() for x in (event_risk.get("display_lines") or []))
        if secondary_line.lower() not in existing_lines_low:
            event_risk["display_lines"].append(secondary_line)

    if merged_summary:
        calendar_summary = build_calendar_risk_summary(merged_events) if merged_events else ""
        summary_line = calendar_summary or merged_summary
        existing_lines = " ".join(event_risk.get("display_lines") or [])
        if summary_line and summary_line not in existing_lines and len(event_risk["display_lines"]) < 3:
            event_risk["display_lines"].append(f"🗓 Сводка макрорисков: {summary_line}")

    if not event_risk.get("active_events"):
        event_risk.pop("active_events", None)

    d["event_risk"] = event_risk


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


def _derive_prompt_structure_levels(ohlcv_tail, *, lookback: int = 20) -> dict:
    if not isinstance(ohlcv_tail, list):
        return {}

    highs: list[float] = []
    lows: list[float] = []
    for row in ohlcv_tail[-lookback:]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        hi = _to_float(row[2])
        lo = _to_float(row[3])
        if hi is not None:
            highs.append(float(hi))
        if lo is not None:
            lows.append(float(lo))

    out: dict[str, float] = {}
    if highs:
        out["recent_high"] = round(max(highs), 6)
    if lows:
        out["recent_low"] = round(min(lows), 6)
    return out


def build_symbol_prompt_technical_context(symbol: str, price: float | None = None) -> dict:
    sym = (symbol or "").strip()
    if not sym:
        return {}

    out: dict = {
        "symbol": sym,
        "price": _round_price(price, symbol=sym) if _to_float(price) is not None else None,
        "timeframes": {},
    }

    warmup_len = max(500, _OHLCV_TAIL_LEN)
    for timeframe in ("15m", "1h", "4h"):
        snap = _fetch_closes_from_market(
            "bybit_swap",
            timeframe,
            symbol=sym,
            limit=warmup_len,
            min_len=20,
        )
        closes = snap.get("closes") if isinstance(snap, dict) else None
        if not isinstance(closes, list) or len(closes) < 60:
            continue

        ema20 = _ema_sma_seed(closes, 20)
        ema60 = _ema_sma_seed(closes, 60)
        ohlcv_tail = snap.get("ohlcv_tail")
        frame_block: dict = {
            "ema20": round(float(ema20), 6) if ema20 is not None else None,
            "ema60": round(float(ema60), 6) if ema60 is not None else None,
            "last_candle": snap.get("last_candle"),
            "structure_levels": _derive_prompt_structure_levels(ohlcv_tail),
        }

        px = _to_float(out.get("price"))
        if px is not None:
            frame_block["price_vs_ema20"] = _ema_relation_flag(px, frame_block.get("ema20"))
            frame_block["price_vs_ema60"] = _ema_relation_flag(px, frame_block.get("ema60"))

        out["timeframes"][timeframe] = frame_block

    return out


def build_pool_prompt_technical_context(pool_snapshot: dict) -> dict:
    out: dict = {}
    for symbol, quote in (pool_snapshot or {}).items():
        price = quote.get("last") if isinstance(quote, dict) else None
        ctx = build_symbol_prompt_technical_context(symbol, price=price)
        if ctx.get("timeframes"):
            out[symbol] = ctx
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
US_OPEN_BLOCK_UTC_WINDOW = "14:00-16:30"  # 17:00–19:30 MSK
US_SESSION_LATE_UTC_WINDOW = "16:30-21:00"  # 19:30–24:00 MSK


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


def _is_us_open_block(d: dict) -> bool:
    """
    Phase 1: US open initial activity window in MSK (17:00–19:30 MSK).
    Implemented in UTC: 14:00–16:30 UTC.
    """
    window = _parse_utc_window_minutes(US_OPEN_BLOCK_UTC_WINDOW)
    if not window:
        return False
    mins_utc = _utc_minutes_from_signal_time(d)
    if mins_utc is None:
        return False
    return _is_in_utc_window(mins_utc, window[0], window[1])


def _is_us_session_late(d: dict) -> bool:
    """
    Phase 2: late US window in MSK (19:30–24:00 MSK).
    Implemented in UTC: 16:30–21:00 UTC.
    """
    window = _parse_utc_window_minutes(US_SESSION_LATE_UTC_WINDOW)
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
    Impulse proxy: deterministic sync with impulse markers.
    Rules:
    - True if warnings contain "impulse_no_exhale" or "phase_between"
    - True if flush/knife detector triggers (existing ATR-based logic)
    - False otherwise
    """
    warnings = d.get("warnings")
    wset: set[str] = set()
    if isinstance(warnings, list):
        for w in warnings:
            s = str(w or "").strip().lower()
            if s:
                wset.add(s)
    if ("impulse_no_exhale" in wset) or ("phase_between" in wset):
        return True
    return bool(_m15_flush_detected(d))


def sync_impulse_proxy(d: dict) -> None:
    """
    Ensure `impulse_proxy` is always present and consistent with final warnings + computed markers.
    """
    if not isinstance(d, dict):
        return
    impulse_proxy = bool(_compute_impulse_proxy(d))
    d["impulse_proxy"] = impulse_proxy
    dbg = d.get("debug")
    if isinstance(dbg, dict):
        dbg["impulse_proxy"] = impulse_proxy


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
    is_us_open_block = _is_us_open_block(d)
    is_us_session_late = _is_us_session_late(d)

    # Persist to top-level JSON (required for logs/last.json consumers).
    # Always booleans; if inputs are missing, compute_* helpers return False.
    d["phase_flip_m15"] = bool(phase_flip_m15)
    d["impulse_proxy"] = bool(impulse_proxy)
    d["is_us_session"] = bool(is_us_session)
    d["is_us_open_block"] = bool(is_us_open_block)
    d["is_us_session_late"] = bool(is_us_session_late)

    dbg = d.setdefault("debug", {})
    if isinstance(dbg, dict):
        dbg["phase_flip_m15"] = bool(phase_flip_m15)
        dbg["impulse_proxy"] = bool(impulse_proxy)
        dbg["is_us_session"] = bool(is_us_session)
        dbg["is_us_open_block"] = bool(is_us_open_block)
        dbg["is_us_session_late"] = bool(is_us_session_late)

    if not (phase_flip_m15 and impulse_proxy):
        return

    mode = normalize_mode(d.get("mode"))

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


def _drop_trade_levels_for_mode(d: dict, mode: str) -> None:
    """
    Output shaping helper: remove mode-specific trade levels (entry/SL/TP/RR/plan)
    when a policy blocks that mode (no impact on internal logic).
    """
    mode = normalize_mode(mode)
    if mode not in VALID_MODES:
        return

    # Mode-specific derived fields used by rendering.
    d.pop(f"entry_price_{mode}", None)

    sl_by_mode = d.get("sl_by_mode")
    if isinstance(sl_by_mode, dict):
        sl_by_mode.pop(mode, None)
        d["sl_by_mode"] = sl_by_mode

    tp_by_mode = d.get("tp_by_mode")
    if isinstance(tp_by_mode, dict):
        tp_by_mode.pop(mode, None)
        d["tp_by_mode"] = tp_by_mode

    rr_by_mode = d.get("rr_by_mode")
    if isinstance(rr_by_mode, dict):
        rr_by_mode.pop(mode, None)
        d["rr_by_mode"] = rr_by_mode

    exit_plan_by_mode = d.get("exit_plan_by_mode")
    if isinstance(exit_plan_by_mode, dict):
        exit_plan_by_mode.pop(mode, None)
        d["exit_plan_by_mode"] = exit_plan_by_mode

    entries = d.get("entries")
    if isinstance(entries, dict):
        entries.pop(mode, None)
        d["entries"] = entries

    # Remove generic top-level levels that represent the currently selected mode.
    # During a strict block, we don't want to emit actionable levels.
    for k in ("entry_range", "entry_price", "sl", "tp1", "tp2", "tp3"):
        d.pop(k, None)


def apply_us_two_phase_policy(d: dict) -> None:
    """
    Two-phase US window in MSK:
    - Phase 1 (17:00–19:30 MSK): strict block for non-aggressive; aggressive forces wait_confirm.
    - Phase 2 (19:30–24:00 MSK): keep existing behavior unchanged (phase_flip + time_window etc).
    """
    if not isinstance(d, dict):
        return

    # Source of truth flags (should already be present after apply_phase_flip_modifier),
    # but compute defensively in case of partial flows.
    is_open_block = bool(d.get("is_us_open_block")) or _is_us_open_block(d)
    is_session_late = bool(d.get("is_us_session_late")) or _is_us_session_late(d)
    d["is_us_open_block"] = bool(is_open_block)
    d["is_us_session_late"] = bool(is_session_late)
    dbg = d.get("debug")
    if isinstance(dbg, dict):
        dbg["is_us_open_block"] = bool(is_open_block)
        dbg["is_us_session_late"] = bool(is_session_late)

    if not is_open_block:
        return

    mode = normalize_mode(d.get("mode"))

    if mode in {"neutral", "conservative"}:
        d["no_trade"] = True
        d.setdefault("no_trade_reasons", [])
        reasons = d.get("no_trade_reasons")
        if not isinstance(reasons, list):
            reasons = []
        reasons = [r for r in reasons if isinstance(r, str) and r.strip()]
        # Make the policy reason primary.
        reasons = ["us_open_block_non_aggressive"] + [r for r in reasons if r != "us_open_block_non_aggressive"]
        d["no_trade_reasons"] = reasons
        d["no_trade_hint"] = "us_open_block_non_aggressive"

        # Do not emit non-aggressive entry/SL/TP payload.
        _drop_trade_levels_for_mode(d, mode)
        return

    if mode == "aggressive" and not bool(d.get("no_trade")):
        d["entry_mode"] = "wait_confirm"
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

    def _ema_mtf_summary(m15_flag: str, h1_flag: str) -> str:
        def _label(flag: str) -> str:
            return {
                "above": "выше EMA20",
                "below": "ниже EMA20",
                "equal": "у EMA20",
            }.get(flag, "относительно EMA20 не определена")

        if m15_flag == h1_flag == "above":
            return "M15 и H1: цена выше EMA20, структура по EMA20 поддерживает рост."
        if m15_flag == h1_flag == "below":
            return "M15 и H1: цена ниже EMA20, структура по EMA20 остаётся слабой."
        return (
            f"M15: цена {_label(m15_flag)}; "
            f"H1: цена {_label(h1_flag)}; "
            "по EMA20 структура смешанная, единого подтверждения нет."
        )

    def _should_replace_mtf_with_ema_summary(text: str) -> bool:
        low = str(text or "").lower()
        if not low.strip():
            return False
        if "ema20" in low or "ema60" in low:
            return True
        return ("m15" in low and "h1" in low) or ("15m" in low and "1h" in low)

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
        joined = " ".join(str(mtf.get(tf, "")) for tf in ("m15", "h1"))
        if _should_replace_mtf_with_ema_summary(joined):
            d["multi_tf_view"] = _ema_mtf_summary(vs_m15, vs_h1)
            mtf = None
        else:
            if "m15" in mtf and isinstance(mtf.get("m15"), str):
                mtf["m15"] = _fix_line(mtf.get("m15", ""), vs_m15)
            if "h1" in mtf and isinstance(mtf.get("h1"), str):
                mtf["h1"] = _fix_line(mtf.get("h1", ""), vs_h1)
            d["multi_tf_view"] = mtf
    elif isinstance(mtf, str) and _should_replace_mtf_with_ema_summary(mtf):
        d["multi_tf_view"] = _ema_mtf_summary(vs_m15, vs_h1)

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

    why = d.get("why_asset")
    if isinstance(why, str) and why.strip():
        low = why.lower()
        comparative = any(token in low for token in ("предпочтительнее", "лучше", "чище", "сильнее"))
        versus = any(token in low for token in (" чем ", " нежели ", " vs ", " против "))
        if comparative and versus and "ema60" in low:
            cleaned = why
            cleaned = re.sub(
                r"(?i)хай[-\s]*бета\s*/\s*актив(?:ы|ов)?\s+с\s+h1\s+(?:выше|над)\s+ema\s*60",
                "хай-беты и активы со смешанной H1-структурой или ниже EMA60",
                cleaned,
            )
            cleaned = re.sub(
                r"(?i)актив(?:ы|ов)?\s+с\s+h1\s+(?:выше|над)\s+ema\s*60",
                "активы со смешанной H1-структурой или ниже EMA60",
                cleaned,
            )
            cleaned = re.sub(
                r"(?i)h1\s+(?:выше|над)\s+ema\s*60",
                "H1 со смешанной структурой или H1 ниже EMA60",
                cleaned,
            )
            d["why_asset"] = cleaned


def _compose_overview_sections(
    overview_lines: list[str],
    *,
    analysis_profile: str,
    flow_derivatives_section: str = "",
) -> list[str]:
    clean_lines = [str(line).strip() for line in overview_lines if str(line).strip()]
    flow_section = str(flow_derivatives_section or "").strip()
    if analysis_profile != "mid" or not flow_section or not clean_lines:
        return ["\n\n".join(clean_lines)] if clean_lines else ([flow_section] if flow_section else [])
    first = clean_lines[0]
    rest = clean_lines[1:]
    sections = [first, flow_section]
    if rest:
        sections.append("\n\n".join(rest))
    return sections


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
    elif price > em15 and price > em1h and side == "short":
        warnings = d.setdefault("warnings", [])
        if "dir_guard_forced_long_by_ema" not in warnings:
            warnings.append("dir_guard_forced_long_by_ema")
        ema_guard = d.get("ema_guard")
        if isinstance(ema_guard, dict):
            note = ema_guard.get("note") or ema_guard.get("comment")
            if not note:
                ema_guard["comment"] = "short_against_ema_uptrend"


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and x == x


def apply_ema_blocks_and_derivatives(d: dict, symbol: str | None) -> None:
    periods = (9, 12, 20, 50, 200)

    def _set_data_error() -> None:
        d["no_trade"] = True
        reasons = d.setdefault("no_trade_reasons", [])
        if isinstance(reasons, list) and "insufficient_ohlcv_for_ema_fan" not in reasons:
            reasons.append("insufficient_ohlcv_for_ema_fan")
        if not isinstance(d.get("no_trade_hint"), str) or not str(d.get("no_trade_hint") or "").strip():
            d["no_trade_hint"] = "insufficient_ohlcv_for_ema_fan"

    if not symbol:
        d.setdefault("ema_m15", {f"ema{p}": None for p in periods})
        d.setdefault("ema_h1", {f"ema{p}": None for p in periods})
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"
        d.setdefault("pivot_ema_hint_by_mode", "ema20")
        _set_data_error()
        return

    warmup_len = max(500, _OHLCV_TAIL_LEN)
    required_candles = _OHLCV_TAIL_LEN

    snap_m15 = _fetch_closes_from_market(
        "bybit_swap",
        "15m",
        symbol=symbol,
        limit=warmup_len,
        min_len=required_candles,
    )
    snap_h1 = _fetch_closes_from_market(
        "bybit_swap",
        "1h",
        symbol=symbol,
        limit=warmup_len,
        min_len=required_candles,
    )

    closes_m15 = snap_m15.get("closes") if isinstance(snap_m15, dict) else None
    closes_h1 = snap_h1.get("closes") if isinstance(snap_h1, dict) else None
    if not (isinstance(closes_m15, list) and len(closes_m15) >= required_candles):
        d.setdefault("ema_m15", {f"ema{p}": None for p in periods})
        d.setdefault("ema_h1", {f"ema{p}": None for p in periods})
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"
        _set_data_error()
        return
    if not (isinstance(closes_h1, list) and len(closes_h1) >= required_candles):
        d.setdefault("ema_m15", {f"ema{p}": None for p in periods})
        d.setdefault("ema_h1", {f"ema{p}": None for p in periods})
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"
        _set_data_error()
        return

    # Persist OHLCV snapshots for downstream consumers (logs/last.json).
    d["exchange"] = "bybit"
    d["market_type"] = "linear_perp"
    d["price_source"] = "last"

    d["timeframe_m15"] = "15m"
    d["candles_m15_count"] = len(closes_m15)
    d["last_candle_m15"] = snap_m15.get("last_candle")
    d["ohlcv_m15_tail"] = snap_m15.get("ohlcv_tail")
    d["closes_m15_tail"] = snap_m15.get("closes_tail")

    d["timeframe_h1"] = "1h"
    d["candles_h1_count"] = len(closes_h1)
    d["last_candle_h1"] = snap_h1.get("last_candle")
    d["ohlcv_h1_tail"] = snap_h1.get("ohlcv_tail")
    d["closes_h1_tail"] = snap_h1.get("closes_tail")

    ema_m15: dict[str, float | None] = {}
    ema_h1: dict[str, float | None] = {}
    for p in periods:
        ema_m15[f"ema{p}"] = _ema_sma_seed(closes_m15, p)
        ema_h1[f"ema{p}"] = _ema_sma_seed(closes_h1, p)

    required_keys = ("ema9", "ema12", "ema20", "ema50")
    if not all(_is_num(ema_m15.get(k)) for k in required_keys):
        d["ema_m15"] = {f"ema{p}": None for p in periods}
        d["ema_h1"] = {f"ema{p}": None for p in periods}
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"
        _set_data_error()
        return
    if not all(_is_num(ema_h1.get(k)) for k in required_keys):
        d["ema_m15"] = {f"ema{p}": None for p in periods}
        d["ema_h1"] = {f"ema{p}": None for p in periods}
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"
        _set_data_error()
        return

    d["ema_m15"] = {k: round(float(v), 6) for (k, v) in ema_m15.items() if k in {f"ema{p}" for p in periods}}
    d["ema_h1"] = {k: round(float(v), 6) for (k, v) in ema_h1.items() if k in {f"ema{p}" for p in periods}}
    d["ema20_m15"] = d["ema_m15"].get("ema20")
    d["ema20_h1"] = d["ema_h1"].get("ema20")

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

    d["ema_fan_m15_state"] = fan_state(d.get("ema_m15") if isinstance(d.get("ema_m15"), dict) else {})
    d["ema_fan_h1_state"] = fan_state(d.get("ema_h1") if isinstance(d.get("ema_h1"), dict) else {})

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


def select_entry_from_range(
    mode: str,
    side: str,
    entry_mode: str | None,
    range_min: float,
    range_max: float,
) -> float:
    """
    Deterministically selects a single entry price from an LLM-provided range.

    Rules:
    - If entry_mode == "wait_confirm": LONG -> min, SHORT -> max
    - Else if mode == "conservative":  LONG -> min, SHORT -> max
    - Else if mode == "neutral":       LONG -> min, SHORT -> max
    - Else if mode == "aggressive":    midpoint = (min + max) / 2
    - Else (default):                  midpoint = (min + max) / 2
    """
    m = normalize_mode(mode)
    s = (side or "").strip().lower()
    em = (entry_mode or "").strip().lower()

    lo = float(range_min)
    hi = float(range_max)
    if hi < lo:
        lo, hi = hi, lo

    if s not in ("long", "short"):
        return (lo + hi) / 2.0

    if em == "wait_confirm":
        return lo if s == "long" else hi
    if m in ("conservative", "neutral"):
        return lo if s == "long" else hi
    return (lo + hi) / 2.0


def apply_entry_prices_from_ranges(d: dict) -> None:
    symbol = d.get("symbol")
    side = (d.get("side") or d.get("direction") or "").strip().lower()

    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}

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

    def _get_mode_entry_mode(mode: str) -> str | None:
        bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}
        em = bucket.get("entry_mode") if isinstance(bucket, dict) else None
        if isinstance(em, str) and em.strip():
            return em.strip()
        em0 = d.get("entry_mode")
        if isinstance(em0, str) and em0.strip():
            return em0.strip()
        return None

    def _get_mode_range_minmax(mode: str) -> tuple[float, float] | None:
        # Priority:
        # 1) entries[mode].range
        # 2) top-level entry_range
        bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else None
        r1 = (bucket or {}).get("range") if isinstance(bucket, dict) else None
        mm = _range_minmax(r1 if isinstance(r1, dict) else None)
        if mm is not None:
            return mm
        r2 = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else None
        return _range_minmax(r2)

    def _round_dir_for_selected_edge(selected: float, *, lo: float, hi: float) -> str:
        # Keep rounding deterministic and conservative relative to the chosen edge.
        if side == "long":
            return "down" if abs(selected - lo) <= abs(selected - hi) else "up"
        return "up" if abs(selected - hi) <= abs(selected - lo) else "down"

    for mode in ("aggressive", "neutral", "conservative"):
        mm = _get_mode_range_minmax(mode)
        if mm is None:
            # Fallback: keep/normalize existing entry_price_<mode> (if present).
            cur = _to_float(d.get(f"entry_price_{mode}"))
            if cur is None:
                continue
            rounded = _round_price(cur, symbol=symbol)
            d[f"entry_price_{mode}"] = float(rounded if rounded is not None else cur)
            continue

        lo, hi = mm
        em = _get_mode_entry_mode(mode)
        selected = select_entry_from_range(mode, side, em, lo, hi)
        if side not in ("long", "short"):
            v = _round_price(selected, symbol=symbol)
            d[f"entry_price_{mode}"] = float(v if v is not None else selected)
            continue

        # Aggressive midpoint: quantize with unbiased rounding.
        if normalize_mode(mode) == "aggressive" and (em or "").strip().lower() != "wait_confirm":
            v = _round_price(selected, symbol=symbol)
        else:
            rdir = _round_dir_for_selected_edge(selected, lo=lo, hi=hi)
            v = _round_price_dir(selected, rdir, symbol=symbol)
        vv = float(v if v is not None else selected)
        vv = float(_clamp_to(mm, vv))
        d[f"entry_price_{mode}"] = vv


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

    # RR policy:
    # - aggressive: permissive (may still proceed with warnings)
    # - neutral: standard
    # - conservative: strict, but should be producible:
    #   * with numeric TP2 target: require higher RR
    #   * with trail: require minimal RR to TP1 (trail handles the rest)
    rr_min_by_mode = {"aggressive": 1.0, "neutral": 1.5, "conservative": 1.5}
    conservative_min_rr_tp1_trail = 1.0
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

        min_rr_required = rr_min_by_mode[mode]
        is_conservative_trail = False
        if mode == "conservative":
            tvh2_or_trail = out_bucket.get("tvh2_or_trail")
            is_conservative_trail = isinstance(tvh2_or_trail, str) and tvh2_or_trail.strip().lower() == "trail"
            if is_conservative_trail:
                min_rr_required = float(conservative_min_rr_tp1_trail)

        if mode == active_mode and (rr_val is None or rr_val < min_rr_required):
            rr_ok_for_active_mode = False
        elif mode == active_mode and mode == "conservative" and is_conservative_trail:
            # Conservative with trail: we accept minimal RR to TP1 and assume trailing takes over.
            d.setdefault("warnings", [])
            if isinstance(d.get("warnings"), list) and "conservative_trail_rr_assumed" not in d["warnings"]:
                d["warnings"].append("conservative_trail_rr_assumed")

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
        elif active_mode == "neutral":
            # Neutral: low RR is a quality issue, not a hard blocker.
            # Tactic: wait for deeper entry / better RR; do not no_trade on RR alone.
            d.setdefault("warnings", [])
            if isinstance(d.get("warnings"), list) and "neutral_wait_confirm_due_to_rr" not in d["warnings"]:
                d["warnings"].append("neutral_wait_confirm_due_to_rr")
            if (d.get("entry_mode") or "").strip().lower() != "wait_confirm":
                d["entry_mode"] = "wait_confirm"
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


def _entry_anchor_for_tp1_guard(d: dict, mode: str) -> tuple[float | None, str]:
    symbol = d.get("symbol")

    entry = _to_float(d.get(f"entry_price_{mode}"))
    if entry is not None:
        return float(entry), f"entry_price_{mode}"

    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}

    entry = _to_float(bucket.get("entry_price"))
    if entry is not None:
        return float(entry), f"entries.{mode}.entry_price"

    mid = _mid_from_range(bucket.get("range"), symbol=symbol)
    if mid is not None:
        return float(mid), f"entries.{mode}.range_mid"

    entry = _to_float(d.get("entry_price"))
    if entry is not None:
        return float(entry), "entry_price"

    mid = _mid_from_range(d.get("entry_range"), symbol=symbol)
    if mid is not None:
        return float(mid), "entry_range_mid"

    price = _to_float(d.get("price"))
    if price is not None:
        return float(price), "price_fallback"

    return None, ""


def _tp1_net_move_pct(entry: float, tp1: float, *, is_long: bool) -> float | None:
    if entry <= 0:
        return None
    move = (tp1 - entry) / entry if is_long else (entry - tp1) / entry
    return float(move)


def _contract_target_price(entry: float, move_pct: float, *, is_long: bool, symbol=None) -> float:
    raw = entry * (1.0 + move_pct if is_long else 1.0 - move_pct)
    rounded = _round_price(raw, symbol=symbol)
    return float(rounded if rounded is not None else raw)


def _ensure_monotonic_targets(
    entry: float,
    a: float,
    b: float | None,
    c: float | None,
    *,
    is_long: bool,
) -> tuple[float, float | None, float | None]:
    min_step = max(abs(entry) * 0.001, abs(entry) * 0.0001)
    if is_long:
        if not (a > entry):
            a = entry + min_step
        if b is not None and not (b > a):
            b = a + min_step
        if c is not None:
            prev = b if b is not None else a
            if not (c > prev):
                c = prev + min_step
    else:
        if not (a < entry):
            a = entry - min_step
        if b is not None and not (b < a):
            b = a - min_step
        if c is not None:
            prev = b if b is not None else a
            if not (c < prev):
                c = prev - min_step
    return a, b, c


def _normalize_official_target(
    entry: float,
    candidate,
    *,
    move_pct: float,
    is_long: bool,
    symbol=None,
) -> float:
    contract = _contract_target_price(entry, move_pct, is_long=is_long, symbol=symbol)
    value = _to_float(candidate)
    if value is None:
        return contract
    if is_long and value <= entry:
        return contract
    if (not is_long) and value >= entry:
        return contract
    move = _tp1_net_move_pct(entry, value, is_long=is_long)
    if move is None or move < move_pct:
        return contract
    return max(float(value), contract) if is_long else min(float(value), contract)


def _rr_target_from_mode_bucket(mode: str, bucket: dict) -> float | None:
    if not isinstance(bucket, dict):
        return None
    if mode == "aggressive":
        return (
            _to_float(bucket.get("tvh3"))
            or _to_float(bucket.get("tp3"))
            or _to_float(bucket.get("tvh2"))
            or _to_float(bucket.get("tp2"))
            or _to_float(bucket.get("tvh1"))
            or _to_float(bucket.get("tp1"))
        )
    if mode == "neutral":
        return (
            _to_float(bucket.get("tvh2"))
            or _to_float(bucket.get("tp2"))
            or _to_float(bucket.get("tvh1"))
            or _to_float(bucket.get("tp1"))
        )
    return (
        _to_float(bucket.get("tvh2_or_trail"))
        or _to_float(bucket.get("tp2"))
        or _to_float(bucket.get("tvh1"))
        or _to_float(bucket.get("tp1"))
    )


def restore_mode_target_ladder(d: dict) -> dict:
    """
    Restore the official mode target ladder before the final TP1 sanity check.
    Nearby technical levels may remain useful as reaction context, but not as undersized official TP1.
    """
    if not isinstance(d, dict) or bool(d.get("no_trade")):
        return d

    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in {"long", "short"}:
        return d
    is_long = side == "long"
    symbol = d.get("symbol")

    tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
    if not isinstance(tp_by_mode, dict):
        return d
    sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
    rr_by_mode = d.get("rr_by_mode") if isinstance(d.get("rr_by_mode"), dict) else {}

    changed_modes: list[str] = []

    for mode in ("aggressive", "neutral", "conservative"):
        entry, _ = _entry_anchor_for_tp1_guard(d, mode)
        if entry is None:
            continue

        bucket_in = tp_by_mode.get(mode)
        bucket = copy.deepcopy(bucket_in) if isinstance(bucket_in, dict) else {}
        original_bucket = copy.deepcopy(bucket)

        tp1_raw = bucket.get("tvh1", bucket.get("tp1"))
        tp1_val = _normalize_official_target(
            float(entry),
            tp1_raw,
            move_pct=TP1_MIN_NET_MOVE_PCT,
            is_long=is_long,
            symbol=symbol,
        )

        if mode == "aggressive":
            tp2_raw = bucket.get("tvh2", bucket.get("tp2"))
            tp2_val = _normalize_official_target(
                float(entry),
                tp2_raw,
                move_pct=0.02,
                is_long=is_long,
                symbol=symbol,
            )
            tp3_raw = bucket.get("tvh3", bucket.get("tp3"))
            tp3_val = _to_float(tp3_raw)
            if tp3_val is not None:
                tp3_val = _normalize_official_target(
                    float(entry),
                    tp3_val,
                    move_pct=0.03,
                    is_long=is_long,
                    symbol=symbol,
                )
            tp1_val, tp2_val, tp3_val = _ensure_monotonic_targets(
                float(entry),
                float(tp1_val),
                float(tp2_val),
                float(tp3_val) if tp3_val is not None else None,
                is_long=is_long,
            )
            bucket["tvh1"] = float(tp1_val)
            bucket["tvh2"] = float(tp2_val)
            bucket["tvh3"] = float(tp3_val) if tp3_val is not None else None
            if "tp1" in bucket:
                bucket["tp1"] = float(tp1_val)
            if "tp2" in bucket:
                bucket["tp2"] = float(tp2_val)
            if "tp3" in bucket:
                bucket["tp3"] = float(tp3_val) if tp3_val is not None else None
        elif mode == "neutral":
            tp2_raw = bucket.get("tvh2", bucket.get("tp2"))
            tp2_val = _normalize_official_target(
                float(entry),
                tp2_raw,
                move_pct=0.02,
                is_long=is_long,
                symbol=symbol,
            )
            tp1_val, tp2_val, _ = _ensure_monotonic_targets(
                float(entry),
                float(tp1_val),
                float(tp2_val),
                None,
                is_long=is_long,
            )
            bucket["tvh1"] = float(tp1_val)
            bucket["tvh2"] = float(tp2_val)
            if "tp1" in bucket:
                bucket["tp1"] = float(tp1_val)
            if "tp2" in bucket:
                bucket["tp2"] = float(tp2_val)
        else:
            bucket["tvh1"] = float(tp1_val)
            if "tp1" in bucket:
                bucket["tp1"] = float(tp1_val)
            tp2_or_trail = bucket.get("tvh2_or_trail")
            if isinstance(tp2_or_trail, str) and tp2_or_trail.strip().lower() == "trail":
                bucket["tvh2_or_trail"] = "trail"
            else:
                tp2_raw = tp2_or_trail if tp2_or_trail is not None else bucket.get("tp2")
                tp2_val = _normalize_official_target(
                    float(entry),
                    tp2_raw,
                    move_pct=0.02,
                    is_long=is_long,
                    symbol=symbol,
                )
                tp1_val, tp2_val, _ = _ensure_monotonic_targets(
                    float(entry),
                    float(tp1_val),
                    float(tp2_val),
                    None,
                    is_long=is_long,
                )
                bucket["tvh1"] = float(tp1_val)
                bucket["tvh2_or_trail"] = float(tp2_val)
                if "tp2" in bucket:
                    bucket["tp2"] = float(tp2_val)

        if bucket != original_bucket:
            changed_modes.append(mode)
        tp_by_mode[mode] = bucket

        sl_val = _to_float(sl_by_mode.get(mode))
        rr_target = _rr_target_from_mode_bucket(mode, bucket)
        if sl_val is not None and rr_target is not None:
            risk = abs(float(entry) - float(sl_val))
            if risk > 0:
                rr_by_mode[mode] = float(round(abs(float(rr_target) - float(entry)) / risk, 3))

    if changed_modes:
        d.setdefault("warnings", [])
        for mode in changed_modes:
            _append_unique_str(d, "warnings", f"mode_target_ladder_restored:{mode}")
    d["tp_by_mode"] = tp_by_mode
    if rr_by_mode:
        d["rr_by_mode"] = rr_by_mode
    return d


def _sync_top_level_trade_levels_for_mode(d: dict) -> None:
    try:
        symbol = d.get("symbol")
        final_mode = normalize_mode(d.get("mode"))
        entry_val = _to_float(d.get(f"entry_price_{final_mode}"))
        if entry_val is None:
            entry_val, _ = _entry_anchor_for_tp1_guard(d, final_mode)
        if entry_val is not None and entry_val:
            rounded = _round_price(entry_val, symbol=symbol)
            entry_out = float(rounded if rounded is not None else entry_val)
            d["entry_price"] = entry_out
            d["entry"] = entry_out

        entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
        mode_bucket = entries.get(final_mode) if isinstance(entries.get(final_mode), dict) else {}
        mode_range = mode_bucket.get("range") if isinstance(mode_bucket.get("range"), dict) else None
        if isinstance(mode_range, dict) and ("min" in mode_range or "max" in mode_range):
            d["entry_range"] = mode_range

        rr_by_mode = d.get("rr_by_mode") if isinstance(d.get("rr_by_mode"), dict) else {}
        rr_val = _to_float(rr_by_mode.get(final_mode))
        if rr_val is not None and rr_val > 0:
            d["rr"] = float(rr_val)

        sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
        sl_val = _to_float(sl_by_mode.get(final_mode))
        if sl_val is not None and sl_val:
            rounded = _round_price(sl_val, symbol=symbol)
            d["sl"] = float(rounded if rounded is not None else sl_val)

        tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
        tp_bucket = tp_by_mode.get(final_mode) if isinstance(tp_by_mode.get(final_mode), dict) else {}

        tp1_val = _to_float(tp_bucket.get("tvh1"))
        if tp1_val is None:
            tp1_val = _to_float(tp_bucket.get("tp1"))
        if tp1_val is not None and tp1_val:
            rounded = _round_price(tp1_val, symbol=symbol)
            d["tp1"] = float(rounded if rounded is not None else tp1_val)

        tp2_val = _to_float(tp_bucket.get("tvh2"))
        if tp2_val is None:
            tp2_val = _to_float(tp_bucket.get("tp2"))
        if tp2_val is None:
            tp2_val = _to_float(tp_bucket.get("tvh2_or_trail"))
        if tp2_val is not None and tp2_val:
            rounded = _round_price(tp2_val, symbol=symbol)
            d["tp2"] = float(rounded if rounded is not None else tp2_val)

        tp3_val = _to_float(tp_bucket.get("tvh3"))
        if tp3_val is None:
            tp3_val = _to_float(tp_bucket.get("tp3"))
        if tp3_val is not None and tp3_val:
            rounded = _round_price(tp3_val, symbol=symbol)
            d["tp3"] = float(rounded if rounded is not None else tp3_val)
        tp_out = {"tp1": d.get("tp1"), "tp2": d.get("tp2")}
        if d.get("tp3") is not None:
            tp_out["tp3"] = d.get("tp3")
        d["tp"] = tp_out
    except Exception:
        pass


def apply_tp1_min_move_guard(d: dict) -> dict:
    """
    Final publish guard: TP1 must provide at least 1% clean movement from entry.
    Uses the explicit entry anchor when present; falls back to the active range midpoint.
    Does not invent new targets: only promotes an existing next target when available.
    """
    if not isinstance(d, dict) or bool(d.get("no_trade")):
        return d

    restore_mode_target_ladder(d)

    mode = normalize_mode(d.get("mode"))
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if mode not in VALID_MODES or side not in {"long", "short"}:
        return d

    tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
    tp_bucket = tp_by_mode.get(mode) if isinstance(tp_by_mode.get(mode), dict) else None
    if not isinstance(tp_bucket, dict):
        return d

    entry, entry_source = _entry_anchor_for_tp1_guard(d, mode)
    if entry is None:
        return d

    tp1 = _to_float(tp_bucket.get("tvh1"))
    if tp1 is None:
        tp1 = _to_float(tp_bucket.get("tp1"))
    if tp1 is None:
        return d

    is_long = side == "long"
    move_pct = _tp1_net_move_pct(float(entry), float(tp1), is_long=is_long)
    if move_pct is None or move_pct >= TP1_MIN_NET_MOVE_PCT:
        _sync_top_level_trade_levels_for_mode(d)
        return d

    next_key = "tvh2_or_trail" if mode == "conservative" else "tvh2"
    next_tp = _to_float(tp_bucket.get(next_key))
    if next_tp is None and next_key != "tp2":
        next_tp = _to_float(tp_bucket.get("tp2"))
    next_move_pct = _tp1_net_move_pct(float(entry), float(next_tp), is_long=is_long) if next_tp is not None else None

    if next_tp is not None and next_move_pct is not None and next_move_pct >= TP1_MIN_NET_MOVE_PCT:
        symbol = d.get("symbol")
        rounded = _round_price(next_tp, symbol=symbol)
        promoted = float(rounded if rounded is not None else next_tp)
        tp_bucket["tvh1"] = promoted
        if "tp1" in tp_bucket:
            tp_bucket["tp1"] = promoted
        if mode == "aggressive":
            far_tp = _to_float(tp_bucket.get("tvh3"))
            if far_tp is None:
                far_tp = _to_float(tp_bucket.get("tp3"))
            if far_tp is not None:
                far_rounded = _round_price(far_tp, symbol=symbol)
                shifted = float(far_rounded if far_rounded is not None else far_tp)
                tp_bucket["tvh2"] = shifted
                if "tp2" in tp_bucket:
                    tp_bucket["tp2"] = shifted
        elif mode == "conservative" and next_key == "tvh2_or_trail":
            tp_bucket["tvh2_or_trail"] = tp_bucket.get("tvh2_or_trail", "trail")

        tp_by_mode[mode] = tp_bucket
        d["tp_by_mode"] = tp_by_mode
        d.setdefault("warnings", [])
        _append_unique_str(d, "warnings", f"tp1_min_move_guard_adjusted:{mode}:{entry_source}")
        _sync_top_level_trade_levels_for_mode(d)
        return d

    reason_code = "tp1_below_min_move"
    hint = (
        "Рынок уже слишком близко к ближайшим целям, поэтому вход не даёт нормального запаса по потенциалу. "
        "Нужен либо откат к более выгодной зоне входа, либо расширение диапазона/подтверждённый пробой."
    )

    d["no_trade"] = True
    reasons = d.get("no_trade_reasons")
    if not isinstance(reasons, list):
        reasons = []
    reasons = [r for r in reasons if str(r or "").strip() and str(r).strip() != "waiting_confirmation"]
    reasons = [reason_code] + [r for r in reasons if r != reason_code]
    d["no_trade_reasons"] = reasons
    d["no_trade_hint"] = hint
    d.setdefault("warnings", [])
    _append_unique_str(d, "warnings", f"tp1_min_move_guard_failed:{mode}:{entry_source}")
    return d


def validate_active_mode_setup(d: dict) -> dict:
    """
    Mode-specific validation (render contract):
    - проверяет, что для текущего режима есть entry/sl/tp/rr/exit_plan;
    - НЕ меняет торговую логику и НЕ пересчитывает уровни;
    - при невалидности помечает no_trade с понятным комментарием.
    """
    _sanitize_signal_horizon_wording(d)

    # Runtime safety: LLM (or other callers) can emit `warnings` with an invalid type (e.g. str/null).
    # Many downstream gates rely on `warnings` being a list for append semantics.
    w0 = d.get("warnings")
    if not isinstance(w0, list):
        if isinstance(w0, str) and w0.strip():
            d["warnings"] = [w0.strip()]
        else:
            d["warnings"] = []

    def _strip_neutral_trade_payload() -> None:
        d.pop("entry_price_neutral", None)
        try:
            sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else None
            if isinstance(sl_by_mode, dict):
                sl_by_mode.pop("neutral", None)
            tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else None
            if isinstance(tp_by_mode, dict):
                tp_by_mode.pop("neutral", None)
            rr_by_mode = d.get("rr_by_mode") if isinstance(d.get("rr_by_mode"), dict) else None
            if isinstance(rr_by_mode, dict):
                rr_by_mode.pop("neutral", None)
            ep_by_mode = d.get("exit_plan_by_mode") if isinstance(d.get("exit_plan_by_mode"), dict) else None
            if isinstance(ep_by_mode, dict):
                ep_by_mode.pop("neutral", None)
        except Exception:
            pass
        # Legacy single-mode fields (suppress in no-trade payload).
        for k in ("sl", "tp1", "tp2", "tp3"):
            d.pop(k, None)

    def _ensure_aggressive_option_only(note: str) -> None:
        # Use existing math only; do not derive new levels.
        try:
            _ensure_aggressive_option_note(d, note, force=True)
        except Exception:
            pass

    # Neutral gate (must apply even if upstream already set no_trade, e.g. waiting_confirmation).
    # Condition: impulse + phase flip detected, but price has NOT reclaimed EMA20(M15).
    mode_now = normalize_mode(d.get("mode"))
    if mode_now == "neutral":
        vs_m15_now = str(d.get("price_vs_ema20_m15") or "").strip().lower()
        if bool(d.get("impulse_proxy")) and bool(d.get("phase_flip_m15")) and vs_m15_now != "above":
            _set_no_trade_primary_reason(d, "neutral_flip_without_reclaim_forbidden")
            _strip_neutral_trade_payload()
            _ensure_aggressive_option_only(
                "Разворот после импульса (phase flip) без закрепления выше EMA20(M15): "
                "neutral запрещён; допустимо только в aggressive (лучше wait_confirm)."
            )
            _sanitize_signal_horizon_wording(d)
            return d

        # ---- Neutral (STRICT): hard block EMA-direction conflicts ----
        # If the direction guard already flagged an EMA-structure conflict, neutral must not trade it.
        # In that case we emit ONLY an aggressive_option (if it exists) and strip neutral trade levels.
        warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else []
        side_now = (d.get("side") or d.get("direction") or "").strip().lower()
        ema_conflict = bool(
            (side_now == "long" and "dir_guard_forced_short_by_ema" in warnings)
            or (side_now == "short" and "dir_guard_forced_long_by_ema" in warnings)
        )
        if ema_conflict:
            d["no_trade_reason"] = "neutral_direction_conflict_with_ema"
            _set_no_trade_primary_reason(d, "neutral_direction_conflict_with_ema")
            _strip_neutral_trade_payload()
            try:
                _ensure_aggressive_option_note(
                    d,
                    "Neutral запрещён против EMA-структуры; возможен только aggressive (лучше wait_confirm).",
                    force=True,
                )
            except Exception:
                pass
            _sanitize_signal_horizon_wording(d)
            return d

    if bool(d.get("no_trade")):
        # Even when blocked upstream, conservative keeps a fixed horizon contract.
        if normalize_mode(d.get("mode")) == "conservative":
            d["intended_horizon_hours"] = {"min": 24, "max": 72}
        _sanitize_signal_horizon_wording(d)
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
            _sanitize_signal_horizon_wording(d)
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

    # ---- DAY/MID dual-neutral long penalty (soft) ----
    # When both higher-timeframe biases are neutral, avoid default long drift unless
    # local structure is explicitly bullish enough. Keep the setup alive, but require confirmation.
    if final_mode in {"aggressive", "neutral"} and not bool(d.get("no_trade")):
        ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
        day_bias = str(ctx.get("day_bias") or "").strip().lower()
        mid_bias = str(ctx.get("mid_bias") or "").strip().lower()
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if side == "long" and day_bias == "neutral" and mid_bias == "neutral":
            warnings = d.setdefault("warnings", [])
            vs_m15 = str(d.get("price_vs_ema20_m15") or "").strip().lower()
            vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
            fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
            fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()

            strong_bull_structure = bool(
                (vs_m15 == "above" and fan_m15 == "bull")
                or (vs_h1 == "above" and fan_h1 == "bull")
                or (vs_m15 == "above" and vs_h1 == "above" and fan_h1 != "bear")
            )
            ema_conflict = isinstance(warnings, list) and "dir_guard_forced_short_by_ema" in warnings
            if ema_conflict or not strong_bull_structure:
                if isinstance(warnings, list) and "dual_neutral_long_requires_confirmation" not in warnings:
                    warnings.append("dual_neutral_long_requires_confirmation")
                if (d.get("entry_mode") or "").strip().lower() != "wait_confirm":
                    d["entry_mode"] = "wait_confirm"

    # ---- Aggressive extreme blockers + disciplined countertrend handling ----
    if final_mode == "aggressive" and not bool(d.get("no_trade")):
        if bool(d.get("risk_off")):
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and "risk_off" not in reasons:
                reasons.append("risk_off")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "risk_off"
            _sanitize_signal_horizon_wording(d)
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

        # Explicit EMA direction guard: aggressive can still give signals, but must not auto-enter
        # against EMA structure (no blind knife-catching). Confirmation is mandatory.
        warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else []
        ema_conflict = bool(
            (side == "long" and "dir_guard_forced_short_by_ema" in warnings)
            or (side == "short" and "dir_guard_forced_long_by_ema" in warnings)
        )
        if ema_conflict:
            d.setdefault("warnings", [])
            if (
                isinstance(d.get("warnings"), list)
                and "aggressive_direction_conflict_with_ema_wait_confirm" not in d["warnings"]
            ):
                d["warnings"].append("aggressive_direction_conflict_with_ema_wait_confirm")
            d["entry_mode"] = "wait_confirm"

        if side in ("long", "short") and _m15_flush_detected(d) and not _has_reversal_evidence():
            d["no_trade"] = True
            reasons = d.setdefault("no_trade_reasons", [])
            if isinstance(reasons, list) and "flush_knife_aggressive_extreme" not in reasons:
                reasons.append("flush_knife_aggressive_extreme")
            if not (d.get("no_trade_hint") or "").strip():
                d["no_trade_hint"] = "flush_knife_aggressive_extreme"
            _sanitize_signal_horizon_wording(d)
            return d

        strong_h1_up = (vs_h1 == "above") and (fan_h1 == "bull")
        strong_h1_down = (vs_h1 == "below") and (fan_h1 == "bear")
        countertrend = (side == "short" and strong_h1_up) or (side == "long" and strong_h1_down)
        if countertrend:
            vs_m15 = str(d.get("price_vs_ema20_m15") or "").strip().lower()
            ema20_m15 = _to_float(d.get("ema20_m15"))
            closes = d.get("closes_m15_tail")

            last2_above = False
            last2_below = False
            if ema20_m15 is not None and isinstance(closes, list) and len(closes) >= 2:
                last2: list[float] = []
                for x in closes[-2:]:
                    v = _to_float(x)
                    if v is None:
                        last2 = []
                        break
                    last2.append(v)
                if len(last2) == 2:
                    last2_above = bool(last2[0] > ema20_m15 and last2[1] > ema20_m15)
                    last2_below = bool(last2[0] < ema20_m15 and last2[1] < ema20_m15)

            impulse_proxy = bool(d.get("impulse_proxy"))
            phase_flip_m15 = bool(d.get("phase_flip_m15"))
            has_phase_flip_after_impulse = bool(
                phase_flip_m15
                and impulse_proxy
                and (
                    (side == "long" and vs_m15 == "above")
                    or (side == "short" and vs_m15 == "below")
                )
            )
            reversal_evidence = bool(
                (side == "long" and last2_above)
                or (side == "short" and last2_below)
                or (side == "long" and fan_m15 == "bull")
                or (side == "short" and fan_m15 == "bear")
                or has_phase_flip_after_impulse
            )

            warnings = d.setdefault("warnings", [])
            if isinstance(warnings, list):
                if reversal_evidence:
                    if "aggressive_countertrend_with_evidence" not in warnings:
                        warnings.append("aggressive_countertrend_with_evidence")
                else:
                    d["entry_mode"] = "wait_confirm"
                    if "aggressive_countertrend_no_evidence_wait_confirm" not in warnings:
                        warnings.append("aggressive_countertrend_no_evidence_wait_confirm")

        # Night / low-liquidity window (00:00–07:00 MSK): never hard-block by time,
        # but require explicit risk warning + confirmation-based entry.
        try:
            apply_aggressive_night_low_liquidity_policy(d)
        except Exception:
            pass

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
            _sanitize_signal_horizon_wording(d)
            return d

    # ---- Neutral (STRICT): require stabilization (avoid reversals / knife catches) ----
    if final_mode == "neutral" and not bool(d.get("no_trade")):
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        vs_m15 = str(d.get("price_vs_ema20_m15") or "").strip().lower()
        vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
        fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
        fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
        warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else None
        # Gate can still trigger here if mode fell back into neutral during validation.
        if bool(d.get("impulse_proxy")) and bool(d.get("phase_flip_m15")) and vs_m15 != "above":
            _set_no_trade_primary_reason(d, "neutral_flip_without_reclaim_forbidden")
            _strip_neutral_trade_payload()
            _ensure_aggressive_option_only(
                "Разворот после импульса (phase flip) без закрепления выше EMA20(M15): "
                "neutral запрещён; допустимо только в aggressive (лучше wait_confirm)."
            )
            return d

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
                _sanitize_signal_horizon_wording(d)
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
            _sanitize_signal_horizon_wording(d)
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

        # If the neutral profile provides an entry range, neutral must anchor at the conservative edge:
        #   LONG -> range.min, SHORT -> range.max
        # This keeps neutral meaningfully deeper than near-market / aggressive "enter now" ideas.
        try:
            if side in ("long", "short") and env_mm is not None:
                lo, hi = env_mm
                base_entry = float(lo) if side == "long" else float(hi)
                base_q = _round_price_dir(
                    base_entry,
                    "down" if side == "long" else "up",
                    symbol=d.get("symbol"),
                )
                if base_q is None:
                    base_q = base_entry
                base_q = float(base_q)
                if base_q < float(lo):
                    base_q = float(lo)
                if base_q > float(hi):
                    base_q = float(hi)
                d["entry_price_neutral"] = float(base_q)
        except Exception:
            pass

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

                # Neutral entry must be at the conservative edge (not the mid).
                try:
                    edge = float(new_range["min"]) if side == "long" else float(new_range["max"])
                except Exception:
                    edge = None
                if edge is not None:
                    edge_q = _round_price_dir(
                        float(edge),
                        "down" if side == "long" else "up",
                        symbol=symbol,
                    )
                    if edge_q is None:
                        edge_q = float(edge)
                    d["entry_price_neutral"] = float(edge_q)

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

                offset_needed = (
                    (side == "long" and float(neu0) > float(px) - float(offset_abs))
                    or (side == "short" and float(neu0) < float(px) + float(offset_abs))
                )
                if offset_needed:
                    target_from_current = (
                        float(px) - float(offset_abs) if side == "long" else float(px) + float(offset_abs)
                    )
                    cand_raw = (
                        min(float(neu0), target_from_current)
                        if side == "long"
                        else max(float(neu0), target_from_current)
                    )
                    cand = _round_price_dir(cand_raw, "down" if side == "long" else "up", symbol=d.get("symbol"))
                    if cand is None:
                        cand = cand_raw

                    sl_by_mode = d.get("sl_by_mode") if isinstance(d.get("sl_by_mode"), dict) else {}
                    sl_neutral = _to_float(sl_by_mode.get("neutral"))
                    tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
                    tp_neutral = tp_by_mode.get("neutral") if isinstance(tp_by_mode.get("neutral"), dict) else {}
                    tp1_neutral = _to_float(tp_neutral.get("tvh1"))

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
                        # Do not invert the setup: entry must remain on the correct side of TP1 when present.
                        if tp1_neutral is not None and math.isfinite(float(tp1_neutral)):
                            if side == "long" and float(cand) >= float(tp1_neutral):
                                invalid = True
                            if side == "short" and float(cand) <= float(tp1_neutral):
                                invalid = True

                    if invalid:
                        # Neutral: volatility-based deep offset could not be placed safely.
                        # This is a quality issue → pause (wait_confirm), but do not no_trade unless hard gates block later.
                        if final_mode == "neutral":
                            d.setdefault("warnings", [])
                            if (
                                isinstance(d.get("warnings"), list)
                                and "neutral_wait_confirm_due_to_volatility" not in d["warnings"]
                            ):
                                d["warnings"].append("neutral_wait_confirm_due_to_volatility")
                            if (d.get("entry_mode") or "").strip().lower() != "wait_confirm":
                                d["entry_mode"] = "wait_confirm"

                            # Keep aggressive as an optional early alternative (if already present).
                            existing = d.get("aggressive_option")
                            a_entry = None
                            if isinstance(existing, dict) and _to_float(existing.get("entry_price")) is not None:
                                a_entry = _to_float(existing.get("entry_price"))
                            else:
                                a_entry = _to_float(d.get("entry_price_aggressive"))
                            if a_entry is not None:
                                d["aggressive_option"] = {
                                    "entry_price": float(a_entry),
                                    "note": "Neutral ждёт подтверждение: по волатильности не удалось выставить безопасный глубокий вход; aggressive возможен раньше (повышенный риск).",
                                }
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
                    tp_by_mode = d.get("tp_by_mode") if isinstance(d.get("tp_by_mode"), dict) else {}
                    tp_neutral = tp_by_mode.get("neutral") if isinstance(tp_by_mode.get("neutral"), dict) else {}
                    tp1_neutral = _to_float(tp_neutral.get("tvh1"))

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
        # Neutral must be continuation/patient only; if the entry is still near-market,
        # shift it farther using the already-computed volatility-aware offset.
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
                    offset_abs = _to_float(d.get("neutral_offset_abs"))
                    if offset_abs is None or not (math.isfinite(float(offset_abs)) and float(offset_abs) > 0):
                        is_major = _symbol_is_major(str(symbol or ""))
                        min_pct = NEUTRAL_VOL_MIN_OFFSET_PCT_MAJOR if is_major else NEUTRAL_VOL_MIN_OFFSET_PCT_ALT
                        offset_abs = float(min_pct) * float(px)

                    target_raw = float(px) - float(offset_abs) if side == "long" else float(px) + float(offset_abs)
                    target = _round_price_dir(target_raw, "down" if side == "long" else "up", symbol=symbol)
                    if target is None:
                        target = target_raw

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

                    unsafe = False
                    if not (math.isfinite(float(target)) and float(target) > 0):
                        unsafe = True
                    if mm is not None:
                        lo, hi = mm
                        if float(target) < float(lo) or float(target) > float(hi):
                            unsafe = True
                    if sl_neutral is None or not (math.isfinite(float(sl_neutral)) and float(sl_neutral) > 0):
                        unsafe = True
                    if not unsafe:
                        if side == "long" and not (float(target) > float(sl_neutral)):
                            unsafe = True
                        if side == "short" and not (float(target) < float(sl_neutral)):
                            unsafe = True
                        # Do not invert the setup: entry must remain on the correct side of TP1 when present.
                        if tp1_neutral is not None and math.isfinite(float(tp1_neutral)):
                            if side == "long" and float(target) >= float(tp1_neutral):
                                unsafe = True
                            if side == "short" and float(target) <= float(tp1_neutral):
                                unsafe = True

                    if unsafe:
                        # Neutral: could not safely push away from near-market using volatility offset.
                        # Treat as wait_confirm, keep the idea alive, let hard gates decide no_trade later.
                        if final_mode == "neutral":
                            d.setdefault("warnings", [])
                            if (
                                isinstance(d.get("warnings"), list)
                                and "neutral_wait_confirm_due_to_volatility" not in d["warnings"]
                            ):
                                d["warnings"].append("neutral_wait_confirm_due_to_volatility")
                            if (d.get("entry_mode") or "").strip().lower() != "wait_confirm":
                                d["entry_mode"] = "wait_confirm"

                            existing = d.get("aggressive_option")
                            a_entry = None
                            if isinstance(existing, dict) and _to_float(existing.get("entry_price")) is not None:
                                a_entry = _to_float(existing.get("entry_price"))
                            else:
                                a_entry = _to_float(d.get("entry_price_aggressive"))
                            if a_entry is not None:
                                d["aggressive_option"] = {
                                    "entry_price": float(a_entry),
                                    "note": "Neutral ждёт подтверждение: по волатильности не удалось безопасно отодвинуть вход от текущей; aggressive возможен раньше (повышенный риск).",
                                }

                    if not unsafe:
                        d["entry_price_neutral"] = float(target)
                        warnings = d.setdefault("warnings", [])
                        if isinstance(warnings, list) and "neutral_entry_shifted_by_volatility" not in warnings:
                            warnings.append("neutral_entry_shifted_by_volatility")
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
            _sanitize_signal_horizon_wording(d)
            return d

        ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
        mid_bias = str((ctx or {}).get("mid_bias") or "").strip().lower()
        day_bias = str((ctx or {}).get("day_bias") or "").strip().lower()

        # Proposed side for conservative (may come from MID, but MID can be neutral/missing).
        side = (d.get("side") or d.get("direction") or "").strip().lower()
        if side not in ("long", "short"):
            return _block_cons(
                "conservative_requires_mid_bias",
                "Conservative требует явный side (long/short).",
            )

        mid_is_explicit = mid_bias in ("long", "short")
        effective_side = mid_bias if mid_is_explicit else side

        # MID alignment:
        # - If MID bias is explicit: enforce it strictly (as before).
        # - If MID bias is neutral/missing: allow only when DAY+local are strongly aligned.
        if mid_is_explicit:
            if side != mid_bias:
                return _block_cons(
                    "conservative_requires_mid_bias",
                    "Направление не совпадает с MID bias — conservative пропускает.",
                )
            # DAY alignment: same as MID or neutral (not opposite).
            if day_bias in ("long", "short") and day_bias != mid_bias:
                return _block_cons(
                    "conservative_day_mid_conflict",
                    "DAY bias противоречит MID — conservative пропускает.",
                )
        else:
            # DAY must not oppose the proposed side; neutral is allowed only with strong local alignment.
            if day_bias in ("long", "short") and day_bias != side:
                return _block_cons(
                    "conservative_requires_mid_bias",
                    "MID bias нейтрален/отсутствует, а DAY bias против направления — conservative пропускает.",
                )
            if day_bias not in ("long", "short", "neutral"):
                return _block_cons(
                    "conservative_requires_mid_bias",
                    "MID bias нейтрален/отсутствует и DAY bias не задан — conservative пропускает.",
                )

        # Local stabilization (hard): no flush/knife + no impulse-no-exhale + no adverse fan.
        warnings = d.get("warnings")
        if isinstance(warnings, list):
            wl = " ".join(str(w or "").strip().lower() for w in warnings)
            if "impulse_no_exhale" in wl:
                reason = "conservative_local_not_stable" if mid_is_explicit else "conservative_requires_mid_bias"
                hint = (
                    "Локально нет стабилизации после импульса (impulse_no_exhale)."
                    if mid_is_explicit
                    else "MID bias нейтрален/отсутствует, а локально нет стабилизации после импульса (impulse_no_exhale)."
                )
                return _block_cons(reason, hint)

        if bool(d.get("impulse_proxy")):
            reason = "conservative_local_not_stable" if mid_is_explicit else "conservative_requires_mid_bias"
            hint = (
                "Есть признаки импульса (impulse_proxy) — conservative пропускает."
                if mid_is_explicit
                else "MID bias нейтрален/отсутствует, а локально есть признаки импульса (impulse_proxy) — conservative пропускает."
            )
            return _block_cons(reason, hint)

        if _m15_flush_detected(d):
            reason = "conservative_local_not_stable" if mid_is_explicit else "conservative_requires_mid_bias"
            hint = (
                "Локально риск flush/knife — conservative пропускает."
                if mid_is_explicit
                else "MID bias нейтрален/отсутствует, а локально риск flush/knife — conservative пропускает."
            )
            return _block_cons(reason, hint)

        adverse_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()
        adverse_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
        if effective_side == "long":
            if adverse_m15 == "bear" or adverse_h1 == "bear":
                reason = "conservative_local_not_stable" if mid_is_explicit else "conservative_requires_mid_bias"
                hint = (
                    "EMA fan против направления (bear для LONG) — conservative пропускает."
                    if mid_is_explicit
                    else "MID bias нейтрален/отсутствует, а EMA fan против направления (bear для LONG) — conservative пропускает."
                )
                return _block_cons(reason, hint)
        else:
            if adverse_m15 == "bull" or adverse_h1 == "bull":
                reason = "conservative_local_not_stable" if mid_is_explicit else "conservative_requires_mid_bias"
                hint = (
                    "EMA fan против направления (bull для SHORT) — conservative пропускает."
                    if mid_is_explicit
                    else "MID bias нейтрален/отсутствует, а EMA fan против направления (bull для SHORT) — conservative пропускает."
                )
                return _block_cons(reason, hint)

        # When MID is neutral/missing, additionally require that H1 context is not adverse.
        if not mid_is_explicit:
            vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
            if effective_side == "long" and vs_h1 == "below":
                return _block_cons(
                    "conservative_requires_mid_bias",
                    "MID bias нейтрален/отсутствует, но цена ниже EMA20(H1) — conservative пропускает.",
                )
            if effective_side == "short" and vs_h1 == "above":
                return _block_cons(
                    "conservative_requires_mid_bias",
                    "MID bias нейтрален/отсутствует, но цена выше EMA20(H1) — conservative пропускает.",
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
            deeper_ok = (effective_side == "long" and cons_entry < neu_entry) or (
                effective_side == "short" and cons_entry > neu_entry
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
    ensure_warnings_list(d)
    ensure_macro_event_fields(d)

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
    restore_mode_target_ladder(d)
    apply_tp1_min_move_guard(d)
    _debug_trace_set_entry_range("entry_range_post_tvh", d)
    _sync_top_level_trade_levels_for_mode(d)

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
    try:
        apply_us_two_phase_policy(d)
    except Exception:
        pass
    try:
        sync_impulse_proxy(d)
    except Exception:
        pass
    try:
        apply_upcoming_event_risk(d)
    except Exception:
        pass
    try:
        apply_signal_asset_flow_overlay(d)
    except Exception:
        pass
    _sanitize_signal_horizon_wording(d)
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


def read_latest_report_payload(
    root_dir: str,
    *,
    max_age_hours: float | None = None,
    base_dir: Path | None = None,
) -> dict | None:
    try:
        base_root = base_dir if isinstance(base_dir, Path) else BASE
        base = base_root / "reports" / root_dir
        roots = sorted(base.glob("*"))
        if not roots:
            return None
        report_dir = roots[-1]
        payload_path = report_dir / "last.json"
        if not payload_path.exists():
            return None

        report_time = None
        try:
            report_time = datetime.strptime(report_dir.name, "%Y%m%d_%H%M%S").replace(
                tzinfo=ZoneInfo("Europe/Moscow")
            )
        except Exception:
            report_time = None

        fs_report_time = datetime.fromtimestamp(
            max(report_dir.stat().st_mtime, payload_path.stat().st_mtime),
            tz=ZoneInfo("Europe/Moscow"),
        )
        if report_time is None or fs_report_time > report_time:
            report_time = fs_report_time

        now = datetime.now(ZoneInfo("Europe/Moscow"))
        age_hours = max((now - report_time).total_seconds() / 3600.0, 0.0)
        if max_age_hours is not None and age_hours > float(max_age_hours):
            return None

        raw = json.loads(payload_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None

        ensure_macro_event_fields(raw)
        out = {
            "time_msk": raw.get("time_msk"),
            "upcoming_events": copy.deepcopy(raw.get("upcoming_events") or []),
            "macro_risk_summary": raw.get("macro_risk_summary") or "",
            "event_risk_context": copy.deepcopy(raw.get("event_risk_context") or []),
            "event_risk_context_timestamp_utc": raw.get("event_risk_context_timestamp_utc") or "",
            "day_mid_context": copy.deepcopy(raw.get("day_mid_context")),
            "_report_dir": report_dir.name,
            "_age_hours": age_hours,
            "_source": root_dir,
        }
        return _normalize_report_macro_context(out)
    except Exception:
        return None


def _event_calendar_limit(profile: str) -> int:
    return 7 if _normalize_optional_text(profile).lower() == "mid" else 5


def resolve_event_calendar_profile(
    profile_hint: str | None = None,
    *,
    system_prompt: str | None = None,
) -> str:
    hinted = _normalize_optional_text(profile_hint or os.getenv("EVENT_CALENDAR_PROFILE")).lower()
    if hinted in {"day", "mid", "signal"}:
        return hinted

    prompt_text = _normalize_optional_text(system_prompt).lower()
    if "дневной (1–3 дня)" in prompt_text or "следующие 24 часа" in prompt_text:
        return "day"
    if "среднесрочный (3–7 дней)" in prompt_text or "горизонт: только следующие 3–7 дней" in prompt_text:
        return "mid"
    return "signal"


def load_event_calendar_context(
    profile: str,
    *,
    now_msk=None,
    max_events: int | None = None,
) -> dict:
    return build_calendar_context(
        profile,
        now_msk=now_msk,
        max_events=max_events if max_events is not None else _event_calendar_limit(profile),
    )


def build_event_calendar_prompt_block(
    profile: str,
    *,
    now_msk=None,
    calendar_context: dict | None = None,
) -> str:
    ctx = (
        copy.deepcopy(calendar_context)
        if isinstance(calendar_context, dict)
        else load_event_calendar_context(profile, now_msk=now_msk)
    )
    return (
        "\n=== EVENT CALENDAR (PRIMARY SCHEDULED TIMING SOURCE) ===\n"
        "Ниже — calendar_events из локального event calendar. Это primary source of truth для scheduled event timing.\n"
        "Продолжай обычный market/news analysis exactly as before; calendar_events используй для validation/enrichment тайминга, а не для замены анализа.\n"
        "Если valid calendar event есть в calendar_events, он должен попасть в upcoming_events.\n"
        "If calendar_events contains a valid scheduled item inside the horizon, it must appear in upcoming_events.\n"
        "Если calendar_events пуст, не выдумывай scheduled events; допускается только общий unscheduled risk в macro_risk_summary.\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n"
    )


def merge_event_calendar_context(
    d: dict,
    *,
    profile: str = "signal",
    now_msk=None,
    calendar_context: dict | None = None,
) -> dict:
    if not isinstance(d, dict):
        return {"generated_at_utc": None, "calendar_events": []}

    ensure_macro_event_fields(d)
    _normalize_day_mid_context(d)
    now_dt = _resolve_macro_event_now_dt(now_msk or d.get("time_msk"))
    if now_dt is not None:
        _apply_relevant_macro_event_filter(d, now_dt=now_dt)
        _apply_relevant_macro_event_filter(d.get("day_mid_context"), now_dt=now_dt)

    ctx = (
        copy.deepcopy(calendar_context)
        if isinstance(calendar_context, dict)
        else load_event_calendar_context(profile, now_msk=now_msk)
    )
    calendar_events = ctx.get("calendar_events") if isinstance(ctx, dict) else []
    calendar_summary = build_calendar_risk_summary(calendar_events if isinstance(calendar_events, list) else [])
    if now_dt is not None:
        normalized_events, normalized_summary = _sanitize_macro_event_bundle(
            calendar_events,
            calendar_summary,
            now_dt=now_dt,
        )
    else:
        normalized_events, normalized_summary = _normalize_macro_event_bundle(calendar_events, calendar_summary)
    if not normalized_events and not normalized_summary:
        return ctx

    d["upcoming_events"] = _merge_upcoming_event_lists(normalized_events, d.get("upcoming_events"))
    d["macro_risk_summary"] = _merge_unique_texts(normalized_summary, d.get("macro_risk_summary"), max_fragments=3)

    dm_ctx = d.get("day_mid_context") if isinstance(d.get("day_mid_context"), dict) else {}
    dm_ctx = dict(dm_ctx)
    dm_ctx["upcoming_events"] = _merge_upcoming_event_lists(normalized_events, dm_ctx.get("upcoming_events"))
    dm_ctx["macro_risk_summary"] = _merge_unique_texts(
        normalized_summary,
        dm_ctx.get("macro_risk_summary"),
        max_fragments=3,
    )
    d["day_mid_context"] = dm_ctx
    return ctx


def read_aia_event_risk_context(event_risk_path: Path | None = None) -> dict:
    default = {
        "event_risk_level": "low",
        "event_risk_window_active": False,
        "nearest_event_minutes": None,
        "event_bias": "neutral",
        "upcoming_events": [],
    }
    try:
        path = event_risk_path or Path(
            os.getenv("AIA_EVENT_RISK_CONTEXT_PATH") or "/root/llm-signal-ai-agent/logs/event_risk_context_latest.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return copy.deepcopy(default)
    except Exception:
        return copy.deepcopy(default)

    compact_events = []
    for item in raw.get("upcoming_events") or []:
        if not isinstance(item, dict):
            continue
        compact_events.append(
            {
                "name": item.get("name"),
                "category": item.get("category"),
                "impact": item.get("impact"),
                "minutes_to_event": item.get("minutes_to_event"),
                "time_msk": item.get("time_msk"),
                "date_msk": item.get("date_msk"),
            }
        )
        if len(compact_events) >= 3:
            break

    return {
        "event_risk_level": raw.get("event_risk_level") if raw.get("event_risk_level") in {"low", "medium", "high"} else "low",
        "event_risk_window_active": bool(raw.get("event_risk_window_active")),
        "nearest_event_minutes": raw.get("nearest_event_minutes") if isinstance(raw.get("nearest_event_minutes"), int) else None,
        "event_bias": raw.get("event_bias")
        if raw.get("event_bias") in {"risk_on", "risk_off", "uncertain", "mixed", "neutral"}
        else "neutral",
        "upcoming_events": compact_events,
    }


def _sanitize_flow_market_context(value) -> dict:
    if not isinstance(value, dict):
        return {}
    drivers = value.get("drivers") if isinstance(value.get("drivers"), list) else []
    modifiers = value.get("flow_derivatives_modifiers") if isinstance(value.get("flow_derivatives_modifiers"), dict) else {}
    reason_codes = modifiers.get("reason_codes") if isinstance(modifiers.get("reason_codes"), list) else []
    return {
        "bias": value.get("bias") if value.get("bias") in {"bullish", "bearish", "neutral", "mixed"} else "neutral",
        "confidence": max(0.0, min(float(value.get("confidence")), 1.0))
        if isinstance(value.get("confidence"), (int, float))
        else 0.0,
        "crowding_state": value.get("crowding_state")
        if value.get("crowding_state") in {"long_crowded", "short_crowded", "neutral", "mixed"}
        else "neutral",
        "exchange_pressure": value.get("exchange_pressure")
        if value.get("exchange_pressure") in {"high", "medium", "low", "unavailable"}
        else "unavailable",
        "stablecoin_support": value.get("stablecoin_support")
        if value.get("stablecoin_support") in {"high", "medium", "low", "unavailable"}
        else "unavailable",
        "unlock_pressure": value.get("unlock_pressure")
        if value.get("unlock_pressure") in {"high", "medium", "low", "unavailable"}
        else "unavailable",
        "drivers": [str(item).strip() for item in drivers if isinstance(item, str) and str(item).strip()],
        "summary": str(value.get("summary") or "").strip(),
        "flow_derivatives_modifiers": {
            "directional_bias": modifiers.get("directional_bias")
            if modifiers.get("directional_bias") in {"bullish", "bearish", "neutral"}
            else "neutral",
            "positioning_risk": modifiers.get("positioning_risk")
            if modifiers.get("positioning_risk") in {"low", "medium", "high"}
            else "low",
            "squeeze_risk": modifiers.get("squeeze_risk")
            if modifiers.get("squeeze_risk") in {"low", "medium", "high"}
            else "low",
            "chase_risk": modifiers.get("chase_risk")
            if modifiers.get("chase_risk") in {"low", "medium", "high"}
            else "low",
            "confirmation_required": bool(modifiers.get("confirmation_required")),
            "reason_codes": [str(item).strip() for item in reason_codes if str(item).strip()],
        },
    }


def _parse_flow_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(ZoneInfo("UTC"))


def _flow_freshness_minutes(raw: dict) -> float | None:
    if not isinstance(raw, dict):
        return None
    generated = _parse_flow_timestamp(raw.get("generated_at")) or _parse_flow_timestamp(raw.get("timestamp_utc"))
    if generated is None:
        return None
    delta = datetime.now(ZoneInfo("UTC")) - generated
    return round(max(delta.total_seconds(), 0.0) / 60.0, 1)


def _flow_staleness_limit_minutes(profile: str) -> float:
    profile_key = str(profile or "day").strip().lower()
    return float(FLOW_STALENESS_LIMITS_MINUTES.get(profile_key, FLOW_STALENESS_LIMITS_MINUTES["day"]))


def _build_stale_flow_snapshot(raw: dict, *, market_context: dict | None = None, profile: str = "day") -> dict:
    snapshot = _sanitize_flow_snapshot_metadata(raw)
    context = market_context if isinstance(market_context, dict) else {}
    snapshot["status"] = "stale"
    snapshot["ignored_for_decision"] = True
    snapshot["staleness_limit_minutes"] = _flow_staleness_limit_minutes(profile)
    snapshot["previous_bias"] = str(context.get("bias") or "neutral").strip() or "neutral"
    return snapshot


def _is_flow_snapshot_stale(raw: dict, *, profile: str) -> bool:
    freshness = _flow_freshness_minutes(raw)
    if freshness is None and isinstance(raw.get("freshness_minutes"), (int, float)):
        freshness = float(raw.get("freshness_minutes"))
    if freshness is None:
        return False
    return freshness > _flow_staleness_limit_minutes(profile)


def _sanitize_flow_snapshot_metadata(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("timestamp_utc", "generated_at"):
        text = str(raw.get(key) or "").strip()
        if text:
            out[key] = text
    if raw.get("data_source") in {"live", "fallback", "mixed"}:
        out["data_source"] = raw.get("data_source")
    freshness = _flow_freshness_minutes(raw)
    if freshness is None and isinstance(raw.get("freshness_minutes"), (int, float)):
        freshness = float(raw.get("freshness_minutes"))
    if freshness is not None:
        out["freshness_minutes"] = freshness
    if isinstance(raw.get("input_coverage"), dict):
        out["input_coverage"] = copy.deepcopy(raw.get("input_coverage"))
    if isinstance(raw.get("coverage"), dict):
        out["coverage"] = copy.deepcopy(raw.get("coverage"))
    if isinstance(raw.get("diagnostics"), dict):
        out["diagnostics"] = copy.deepcopy(raw.get("diagnostics"))
    return out


def _normalize_external_context_asset(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if not text:
        return None
    for suffix in ("/USDT", "-USDT", "_USDT"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    for sep in ("/", "-", "_"):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    if text.endswith("USDT") and len(text) > 4:
        text = text[:-4]
    return text or None


def _sanitize_flow_asset_context(value, *, asset_hint: str | None = None) -> dict:
    if not isinstance(value, dict):
        return {}
    raw_context = value.get("flow_derivatives_context") if isinstance(value.get("flow_derivatives_context"), dict) else value
    if not isinstance(raw_context, dict):
        return {}
    asset = _normalize_external_context_asset(value.get("asset")) or _normalize_external_context_asset(asset_hint)
    context = _sanitize_flow_market_context(raw_context)
    if not context:
        return {}
    return {
        "asset": asset,
        "timestamp_utc": _normalize_optional_text(value.get("timestamp_utc")),
        "mode": _normalize_optional_text(value.get("mode")) or "observe_only",
        "flow_derivatives_context": context,
    }


def read_aia_flow_derivatives_context(flow_path: Path | None = None, *, profile: str = "day") -> dict:
    try:
        path = flow_path or Path(
            os.getenv("AIA_FLOW_DERIVATIVES_CONTEXT_PATH") or "/root/llm-signal-ai-agent/logs/flow_derivatives_context_v2.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
    except Exception:
        return {}

    market_context = _sanitize_flow_market_context(raw.get("market_context"))
    if not market_context:
        return {}
    if _is_flow_snapshot_stale(raw, profile=profile):
        return _build_stale_flow_snapshot(raw, market_context=market_context, profile=profile)

    return {
        "status": "live",
        "market_context": market_context,
        **_sanitize_flow_snapshot_metadata(raw),
    }


def _has_valid_flow_market_context_payload(value) -> bool:
    if not isinstance(value, dict):
        return False
    drivers = value.get("drivers")
    if isinstance(drivers, list) and any(isinstance(item, str) and item.strip() for item in drivers):
        return True
    if str(value.get("summary") or "").strip():
        return True
    if value.get("bias") in {"bullish", "bearish", "neutral", "mixed"}:
        return True
    if isinstance(value.get("confidence"), (int, float)):
        return True
    if value.get("crowding_state") in {"long_crowded", "short_crowded", "neutral", "mixed"}:
        return True
    if value.get("exchange_pressure") in {"high", "medium", "low", "unavailable"}:
        return True
    if value.get("stablecoin_support") in {"high", "medium", "low", "unavailable"}:
        return True
    if value.get("unlock_pressure") in {"high", "medium", "low", "unavailable"}:
        return True
    modifiers = value.get("flow_derivatives_modifiers")
    if isinstance(modifiers, dict) and isinstance(modifiers.get("reason_codes"), list) and modifiers.get("reason_codes"):
        return True
    return False


def read_mid_aia_flow_derivatives_context(flow_path: Path | None = None) -> dict:
    snapshot = read_aia_flow_derivatives_context(flow_path, profile="mid")
    if not snapshot:
        return {}
    if snapshot.get("status") == "stale":
        return snapshot
    raw_market_context = {}
    try:
        path = flow_path or Path(
            os.getenv("AIA_FLOW_DERIVATIVES_CONTEXT_PATH") or "/root/llm-signal-ai-agent/logs/flow_derivatives_context_v2.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("market_context"), dict):
            raw_market_context = raw.get("market_context") or {}
    except Exception:
        return {}
    if not _has_valid_flow_market_context_payload(raw_market_context):
        return {}
    return snapshot


def read_signal_asset_flow_context(symbol: str | None, flow_path: Path | None = None) -> dict:
    asset = _normalize_external_context_asset(symbol)
    if asset is None or asset not in FIXED_BOT_ASSET_UNIVERSE:
        return {}
    try:
        path = flow_path or Path(
            os.getenv("AIA_FLOW_DERIVATIVES_CONTEXT_PATH") or "/root/llm-signal-ai-agent/logs/flow_derivatives_context_v2.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
    except Exception:
        return {}
    if _is_flow_snapshot_stale(raw, profile="signal"):
        return {}

    asset_contexts = raw.get("asset_contexts")
    if not isinstance(asset_contexts, dict) or not asset_contexts:
        return {}

    selected = asset_contexts.get(asset)
    if not isinstance(selected, dict):
        selected = None
        for key, value in asset_contexts.items():
            if _normalize_external_context_asset(key) != asset:
                continue
            if isinstance(value, dict):
                selected = value
                break
    if not isinstance(selected, dict):
        return {}
    return _sanitize_flow_asset_context(selected, asset_hint=asset)


def build_flow_derivatives_prompt_block(snapshot: dict | None, *, analysis_profile: str = "day") -> str:
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    if snapshot.get("status") == "stale":
        previous_bias = str(snapshot.get("previous_bias") or "neutral").strip() or "neutral"
        payload = {
            "status": "stale",
            "ignored_for_current_decision": True,
            "generated_at": snapshot.get("generated_at") or snapshot.get("timestamp_utc"),
            "freshness_minutes": snapshot.get("freshness_minutes"),
            "staleness_limit_minutes": snapshot.get("staleness_limit_minutes"),
            "previous_bias": previous_bias,
            "data_source": snapshot.get("data_source"),
        }
        return (
            "\n=== FLOW / DERIVATIVES CONTEXT (EXTERNAL, ADVISORY ONLY) ===\n"
            "Snapshot is stale and must be ignored for the current decision. Do NOT treat the previous flow bias "
            "as active support or resistance for DAY/MID framing.\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n"
        )
    market_context = snapshot.get("market_context") if isinstance(snapshot.get("market_context"), dict) else {}
    if not market_context:
        return ""
    payload = {"market_context": market_context}
    for key in ("timestamp_utc", "generated_at", "data_source", "freshness_minutes", "input_coverage", "coverage", "diagnostics"):
        if key in snapshot:
            payload[key] = snapshot[key]
    if analysis_profile == "mid":
        return (
            "\n=== FLOW / DERIVATIVES CONTEXT (EXTERNAL, ADVISORY ONLY) ===\n"
            "Это внешний snapshot analysis-layer из AIA. Он строится на своей cadence вне MID и не должен "
            "пересчитываться внутри MID. Используй его только как positioning/liquidity overlay для 3–7 day context "
            "и как дополнительную regime nuance.\n"
            "Он НЕ заменяет собственную оценку MID по price action, market structure, macro/news context и НЕ "
            "должен сам по себе переворачивать weekly bias или direction.\n"
            "Если coverage показывает только derivatives, а exchange/stablecoin/tokenomics unavailable, это нужно "
            "назвать прямо: derivatives-only signal, NOT full liquidity-flow confirmation.\n"
            "Если flow bias совпадает с текущим MID view — упомяни подтверждение. Если расходится — опиши это как "
            "underlying support/fragility, less clean downside или squeeze risk, но оставь MID reading первичной. "
            "Если flow mixed/neutral — подчеркни нестабильность и two-sided regime. "
            "При high/severe geopolitical regime flow остаётся вторичным и не должен смягчать downside-shock framing.\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n"
        )
    return (
        "\n=== FLOW / DERIVATIVES CONTEXT (EXTERNAL, ADVISORY ONLY) ===\n"
        "Это внешний snapshot analysis-layer из AIA. Он строится на своей cadence вне DAY и не должен "
        "пересчитываться внутри DAY. Используй его только как positioning/liquidity overlay и дополнительную "
        "regime nuance.\n"
        "Он НЕ заменяет собственную оценку DAY по price action, market structure, macro/news context и НЕ "
        "должен сам по себе переворачивать direction.\n"
        "Если coverage показывает только derivatives, а exchange/stablecoin/tokenomics unavailable, это нужно "
        "назвать прямо: derivatives-only signal, NOT full liquidity-flow confirmation.\n"
        "Если flow bias совпадает с текущим DAY view — упомяни подтверждение. Если расходится — опиши это как "
        "underlying support/fragility, но оставь DAY reading первичной. Если flow mixed/neutral — подчеркни "
        "нестабильность и two-sided risk. При high/severe geopolitical regime flow остаётся вторичным и не "
        "должен оправдывать relaxed buy-the-dip framing.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n"
    )


def render_flow_derivatives_context_section(
    snapshot: dict | None,
    *,
    signal_payload: dict | None = None,
    title: str = "Flow / Derivatives",
    detail_level: str = "normal",
) -> str:
    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    context = snapshot.get("market_context") if isinstance(snapshot.get("market_context"), dict) else {}
    if snapshot.get("status") != "stale" and not context:
        return ""
    detail = str(detail_level or "normal").strip().lower()
    if detail not in {"compact", "day_compact", "normal", "debug"}:
        detail = "normal"

    def _fmt_confidence(value: object) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.2f}"
        return "0.00"

    def _fmt_ratio(live: object, total: object) -> str:
        if isinstance(live, int) and isinstance(total, int):
            return f"{live}/{total}"
        if isinstance(live, (int, float)) and isinstance(total, (int, float)):
            return f"{int(live)}/{int(total)}"
        return "?/?"

    def _fmt_source_summary() -> str:
        parts: list[str] = []
        data_source = str(snapshot.get("data_source") or "").strip()
        if data_source:
            parts.append(f"source={data_source}")
        if isinstance(snapshot.get("freshness_minutes"), (int, float)):
            parts.append(f"freshness={float(snapshot.get('freshness_minutes')):.1f}m")
        return ", ".join(parts)

    grouped_coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}

    def _get_group_source(group_name: str) -> str:
        group = grouped_coverage.get(group_name) if isinstance(grouped_coverage.get(group_name), dict) else {}
        return str(group.get("source") or "").strip().lower() if group else ""

    def _compact_coverage_line() -> str:
        derivatives = grouped_coverage.get("derivatives") if isinstance(grouped_coverage.get("derivatives"), dict) else {}
        derivatives_source = str(derivatives.get("source") or "unavailable").strip()
        assets_live = derivatives.get("assets_live")
        assets_total = derivatives.get("assets_total")
        non_derivatives = []
        for group_name, label in (
            ("exchange_flows", "exchange"),
            ("stablecoin_flows", "stablecoin"),
            ("tokenomics", "tokenomics"),
        ):
            source = _get_group_source(group_name)
            normalized_source = source or "unavailable"
            non_derivatives.append((label, normalized_source))
        line = f"- Покрытие: derivatives {derivatives_source} {_fmt_ratio(assets_live, assets_total)}"
        if non_derivatives:
            if all(source == "unavailable" for _, source in non_derivatives):
                line += "; " + ", ".join(f"{label} unavailable" for label, _ in non_derivatives)
            else:
                ordered_sources = []
                for source in ("live", "fallback", "stale", "unavailable"):
                    if any(item_source == source for _, item_source in non_derivatives):
                        ordered_sources.append(source)
                for _, source in non_derivatives:
                    if source not in ordered_sources:
                        ordered_sources.append(source)
                segments = []
                for source in ordered_sources:
                    labels = [label for label, item_source in non_derivatives if item_source == source]
                    if labels:
                        segments.append(f"{'/'.join(labels)} {source}")
                if segments:
                    line += "; " + "; ".join(segments)
        return line

    def _compact_modifiers_line() -> str:
        modifiers = context.get("flow_derivatives_modifiers") if isinstance(context.get("flow_derivatives_modifiers"), dict) else {}
        return (
            "- Вывод: "
            f"positioning={modifiers.get('directional_bias') or 'neutral'}, "
            f"squeeze_risk={modifiers.get('squeeze_risk') or 'low'}, "
            f"chase_risk={modifiers.get('chase_risk') or 'low'}, "
            f"confirmation_required={'yes' if modifiers.get('confirmation_required') else 'no'}"
        )

    def _day_compact_modifiers_line() -> str:
        modifiers = context.get("flow_derivatives_modifiers") if isinstance(context.get("flow_derivatives_modifiers"), dict) else {}
        return (
            "- Вывод: "
            f"positioning={modifiers.get('directional_bias') or 'neutral'}, "
            f"chase_risk={modifiers.get('chase_risk') or 'low'}, "
            f"confirmation_required={'yes' if modifiers.get('confirmation_required') else 'no'}, "
            f"stablecoin_support={context.get('stablecoin_support') or 'unavailable'}"
        )

    def _derivatives_only_note() -> str:
        if _get_group_source("derivatives") in {"", "unavailable"}:
            return ""
        unavailable = all(
            _get_group_source(group_name) in {"", "unavailable"}
            for group_name in ("exchange_flows", "stablecoin_flows", "tokenomics")
        )
        if not unavailable:
            return ""
        return (
            "Доступен только derivatives-сигнал; exchange/stablecoin/tokenomics недоступны, "
            "поэтому это не полное подтверждение liquidity-flow."
        )

    if snapshot.get("status") == "stale":
        previous_bias = str(snapshot.get("previous_bias") or "neutral").strip() or "neutral"
        generated_at = str(snapshot.get("generated_at") or snapshot.get("timestamp_utc") or "").strip()
        freshness = snapshot.get("freshness_minutes")
        freshness_text = f", freshness={float(freshness):.1f}m" if isinstance(freshness, (int, float)) else ""
        lines = [title]
        lines.append("- Статус: stale, не используется в текущем решении")
        if generated_at:
            lines.append(f"- Последнее обновление: {generated_at}{freshness_text}")
        lines.append(f"- Последний bias: {previous_bias}; snapshot не учитывается, потому что он устарел.")
        return "\n".join(lines)

    signal_line = f"- Сигнал: {context.get('bias') or 'neutral'}, confidence {_fmt_confidence(context.get('confidence'))}"
    source_summary = _fmt_source_summary()
    if source_summary:
        signal_line += f", {source_summary}"

    if detail == "day_compact":
        return "\n".join([title, signal_line, _compact_coverage_line(), _day_compact_modifiers_line()])

    if detail == "compact":
        lines = [title]
        lines.append(signal_line)
        lines.append(_compact_coverage_line())
        lines.append(_compact_modifiers_line())
        note = _derivatives_only_note()
        if note:
            lines.append(f"- Примечание: {note}")
        return "\n".join(lines)

    lines = [title]
    lines.append(f"- Рыночный bias: {context.get('bias') or 'neutral'} (confidence {_fmt_confidence(context.get('confidence'))})")
    source_parts = []
    data_source = str(snapshot.get("data_source") or "").strip()
    if data_source:
        source_parts.append(f"data_source: {data_source}")
    generated_at = str(snapshot.get("generated_at") or snapshot.get("timestamp_utc") or "").strip()
    if generated_at:
        source_parts.append(f"generated_at: {generated_at}")
    if isinstance(snapshot.get("freshness_minutes"), (int, float)):
        source_parts.append(f"freshness: {float(snapshot.get('freshness_minutes')):.1f}m")
    if source_parts:
        lines.append("- Источник: " + " | ".join(source_parts))
    coverage = snapshot.get("input_coverage") if isinstance(snapshot.get("input_coverage"), dict) else {}
    if coverage:
        total = coverage.get("assets_total")
        live = coverage.get("assets_with_live_data")
        fallback = coverage.get("assets_with_fallback_data")
        lines.append(f"- Покрытие входов: total={total} live={live} fallback={fallback}")
    derivatives = grouped_coverage.get("derivatives") if isinstance(grouped_coverage.get("derivatives"), dict) else {}
    if derivatives:
        derivatives_source = str(derivatives.get("source") or "unavailable")
        assets_live = derivatives.get("assets_live")
        assets_total = derivatives.get("assets_total")
        lines.append(f"- Деривативы: {derivatives_source}, {_fmt_ratio(assets_live, assets_total)} активов")
    exchange_flows = grouped_coverage.get("exchange_flows") if isinstance(grouped_coverage.get("exchange_flows"), dict) else {}
    stablecoin_flows = grouped_coverage.get("stablecoin_flows") if isinstance(grouped_coverage.get("stablecoin_flows"), dict) else {}
    tokenomics = grouped_coverage.get("tokenomics") if isinstance(grouped_coverage.get("tokenomics"), dict) else {}
    if exchange_flows:
        lines.append(f"- Биржевые потоки: {exchange_flows.get('source') or 'unavailable'}")
    if stablecoin_flows:
        lines.append(f"- Стейблкоин-потоки: {stablecoin_flows.get('source') or 'unavailable'}")
    if tokenomics:
        lines.append(f"- Токеномика: {tokenomics.get('source') or 'unavailable'}")
    note = _derivatives_only_note()
    if note:
        lines.append("- Примечание: " + note)
    diagnostics = snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    reason = str(diagnostics.get("reason") or "").strip()
    if reason:
        lines.append("- Диагностика: " + reason)
    lines.append(f"- Crowding: {context.get('crowding_state') or 'neutral'}")
    lines.append(f"- Давление на биржи: {context.get('exchange_pressure') or 'unavailable'}")
    lines.append(f"- Поддержка стейблкоинов: {context.get('stablecoin_support') or 'unavailable'}")
    lines.append(f"- Давление unlock: {context.get('unlock_pressure') or 'unavailable'}")
    modifiers = context.get("flow_derivatives_modifiers") if isinstance(context.get("flow_derivatives_modifiers"), dict) else {}
    if modifiers:
        lines.append(
            f"- Позиционирование: {modifiers.get('directional_bias') or 'neutral'}"
            f" | squeeze_risk: {modifiers.get('squeeze_risk') or 'low'}"
            f" | chase_risk: {modifiers.get('chase_risk') or 'low'}"
        )
        lines.append(
            f"- Требуется подтверждение: {'yes' if modifiers.get('confirmation_required') else 'no'}"
            f" | positioning_risk: {modifiers.get('positioning_risk') or 'low'}"
        )
        reason_codes = modifiers.get("reason_codes") if isinstance(modifiers.get("reason_codes"), list) else []
        if reason_codes:
            lines.append("- Коды причин: " + ", ".join(str(item) for item in reason_codes[:8] if str(item).strip()))
    summary = str(context.get("summary") or "").strip()
    if summary:
        lines.append("- Вывод: " + summary)
    return "\n".join(lines)


def _signal_has_clean_continuation_structure(d: dict) -> bool:
    if bool(d.get("no_trade")):
        return False

    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in ("long", "short"):
        return False

    vs_h1 = str(d.get("price_vs_ema20_h1") or "").strip().lower()
    fan_h1 = str(d.get("ema_fan_h1_state") or "").strip().lower()
    fan_m15 = str(d.get("ema_fan_m15_state") or "").strip().lower()

    if side == "long":
        continuation = (vs_h1 == "above") and (fan_h1 == "bull") and (fan_m15 == "bull")
    else:
        continuation = (vs_h1 == "below") and (fan_h1 == "bear") and (fan_m15 == "bear")
    if not continuation:
        return False

    warnings = d.get("warnings") if isinstance(d.get("warnings"), list) else []
    wl = " ".join(str(w or "").strip().lower() for w in warnings)
    if any(token in wl for token in ("impulse_no_exhale", "phase_between", "ema_between_m15_h1", "ema_source_suspect")):
        return False
    if re.search(r"overextended_(no_exhale|h1)\b", wl):
        return False
    return True


def _derive_asset_flow_support_for_direction(
    *,
    side: str,
    asset_bias: str,
    crowding_state: str,
) -> str:
    if side not in {"long", "short"}:
        return "neutral"

    if asset_bias == "mixed":
        support = "mixed"
    elif asset_bias == "neutral":
        support = "neutral"
    elif (side == "long" and asset_bias == "bullish") or (side == "short" and asset_bias == "bearish"):
        support = "supportive"
    else:
        support = "opposed"

    adverse_crowding = (side == "long" and crowding_state == "long_crowded") or (
        side == "short" and crowding_state == "short_crowded"
    )
    if adverse_crowding and support in {"supportive", "neutral"}:
        return "mixed"
    if crowding_state == "mixed" and support == "neutral":
        return "mixed"
    return support


def _derive_signal_asset_flow_overlay_summary(d: dict, snapshot: dict) -> dict:
    default = {
        "summary": {},
        "warnings": [],
        "display_lines": [],
        "prefer_wait_confirm": False,
        "continuation_stricter": False,
        "confidence_up_steps": 0,
        "confidence_down_steps": 0,
    }
    if not isinstance(d, dict) or not isinstance(snapshot, dict):
        return copy.deepcopy(default)

    context = snapshot.get("flow_derivatives_context") if isinstance(snapshot.get("flow_derivatives_context"), dict) else {}
    side = (d.get("side") or d.get("direction") or "").strip().lower()
    if side not in {"long", "short"} or not context:
        return copy.deepcopy(default)

    asset_bias = _normalize_optional_text(context.get("bias")).lower()
    if asset_bias not in {"bullish", "bearish", "neutral", "mixed"}:
        asset_bias = "neutral"
    crowding_state = _normalize_optional_text(context.get("crowding_state")).lower()
    if crowding_state not in {"long_crowded", "short_crowded", "neutral", "mixed"}:
        crowding_state = "neutral"
    exchange_pressure = _normalize_optional_text(context.get("exchange_pressure")).lower()
    if exchange_pressure not in {"high", "medium", "low"}:
        exchange_pressure = "low"
    stablecoin_support = _normalize_optional_text(context.get("stablecoin_support")).lower()
    if stablecoin_support not in {"high", "medium", "low"}:
        stablecoin_support = "low"
    unlock_pressure = _normalize_optional_text(context.get("unlock_pressure")).lower()
    if unlock_pressure not in {"high", "medium", "low"}:
        unlock_pressure = "low"
    flow_confidence = context.get("confidence") if isinstance(context.get("confidence"), (int, float)) else 0.0
    flow_confidence = max(0.0, min(float(flow_confidence), 1.0))

    flow_support = _derive_asset_flow_support_for_direction(
        side=side,
        asset_bias=asset_bias,
        crowding_state=crowding_state,
    )
    event_risk_regime = d.get("event_risk_regime") if isinstance(d.get("event_risk_regime"), dict) else {}
    severe_geopolitical_regime = (
        _normalize_optional_text(event_risk_regime.get("driver")).lower() == "geopolitics"
        and _normalize_optional_text(event_risk_regime.get("severity")).lower() == "severe"
    )
    continuation_setup = _signal_has_clean_continuation_structure(d)
    adverse_crowding = (side == "long" and crowding_state == "long_crowded") or (
        side == "short" and crowding_state == "short_crowded"
    )

    long_tailwind = stablecoin_support == "high" and exchange_pressure in {"low", "medium"} and unlock_pressure != "high"
    short_tailwind = exchange_pressure == "high" and stablecoin_support in {"low", "medium"} and unlock_pressure != "low"

    execution_caution = "low"
    warnings: list[str] = []
    display_lines: list[str] = []
    prefer_wait_confirm = False
    continuation_stricter = False
    confidence_up_steps = 0
    confidence_down_steps = 0

    if flow_support == "opposed":
        execution_caution = "high"
        warnings.append("flow_opposes_direction")
        prefer_wait_confirm = True
        continuation_stricter = True
        confidence_down_steps = 1
        if side == "long":
            display_lines.append("⚠️ Flow opposes this long; confirmation is required.")
        else:
            display_lines.append("⚠️ Flow opposes this short; confirmation is required.")
    elif flow_support == "mixed":
        execution_caution = "medium"
        prefer_wait_confirm = adverse_crowding or continuation_setup
        continuation_stricter = adverse_crowding or continuation_setup
        if crowding_state == "mixed":
            warnings.append("mixed_positioning")
            display_lines.append("⚠️ Mixed positioning increases failed-move risk.")
        elif side == "long" and adverse_crowding:
            display_lines.append("⚠️ Long crowding raises dump risk for fresh longs.")
        elif side == "short" and adverse_crowding:
            display_lines.append("⚠️ Flow opposes fresh continuation shorts; squeeze risk is elevated.")
    else:
        if crowding_state == "mixed":
            execution_caution = "medium"
            warnings.append("mixed_positioning")
            if continuation_setup:
                prefer_wait_confirm = True
                continuation_stricter = True
            display_lines.append("⚠️ Mixed positioning increases failed-move risk.")
        elif flow_support == "supportive":
            execution_caution = "low"
            if side == "long" and long_tailwind and crowding_state != "long_crowded" and flow_confidence >= 0.55:
                confidence_up_steps = 1
            elif side == "short" and short_tailwind and crowding_state != "short_crowded" and flow_confidence >= 0.55:
                confidence_up_steps = 1
            display_lines.append(f"ℹ️ Flow context is supportive for this {side}.")
        else:
            execution_caution = "medium" if crowding_state == "mixed" else "low"

    if side == "long" and crowding_state == "long_crowded":
        warnings.append("dump_risk_long_crowded")
        execution_caution = "high" if flow_support in {"opposed", "mixed"} else "medium"
        prefer_wait_confirm = True
        continuation_stricter = True
        confidence_down_steps = max(confidence_down_steps, 1)
        if not any("dump risk" in line.lower() or "long crowding" in line.lower() for line in display_lines):
            display_lines.append("⚠️ Long crowding raises dump risk for fresh longs.")
    elif side == "short" and crowding_state == "short_crowded":
        warnings.append("squeeze_risk_short_crowded")
        execution_caution = "high" if flow_support in {"opposed", "mixed"} else "medium"
        prefer_wait_confirm = True
        continuation_stricter = True
        confidence_down_steps = max(confidence_down_steps, 1)
        if not any("squeeze risk" in line.lower() for line in display_lines):
            display_lines.append("⚠️ Flow opposes fresh continuation shorts; squeeze risk is elevated.")

    if side == "long" and flow_support != "supportive":
        if exchange_pressure == "high" or stablecoin_support == "low" or unlock_pressure == "high":
            continuation_stricter = True
            prefer_wait_confirm = True
            execution_caution = "high" if flow_support == "opposed" else execution_caution
    if side == "short" and flow_support != "supportive":
        if stablecoin_support == "high" or exchange_pressure == "low":
            continuation_stricter = True
            prefer_wait_confirm = True

    if continuation_setup and continuation_stricter:
        warnings.append("flow_continuation_stricter")
    if execution_caution == "medium" and flow_support == "mixed" and flow_confidence >= 0.65:
        confidence_down_steps = max(confidence_down_steps, 1)
    if severe_geopolitical_regime:
        if confidence_up_steps > 0:
            confidence_up_steps = 0
        if flow_support == "supportive":
            warnings.append("flow_secondary_to_geopolitical_regime")
            display_lines = [
                "ℹ️ Supportive flow remains secondary while severe geopolitical risk keeps continuation tactical-only.",
                *display_lines,
            ]
        prefer_wait_confirm = True
        continuation_stricter = True

    summary = {
        "asset_bias": asset_bias,
        "asset_flow_confidence": round(flow_confidence, 3),
        "crowding_state": crowding_state,
        "flow_support_for_direction": flow_support,
        "execution_caution": execution_caution if execution_caution in {"high", "medium", "low"} else "low",
    }
    if severe_geopolitical_regime:
        summary["secondary_to_geopolitical_regime"] = True
    return {
        "summary": summary,
        "warnings": warnings,
        "display_lines": display_lines[:2],
        "prefer_wait_confirm": bool(prefer_wait_confirm),
        "continuation_stricter": bool(continuation_stricter),
        "confidence_up_steps": int(confidence_up_steps),
        "confidence_down_steps": int(confidence_down_steps),
    }


def apply_signal_asset_flow_overlay(d: dict, flow_path: Path | None = None) -> None:
    if not isinstance(d, dict):
        return
    ensure_warnings_list(d)

    snapshot = read_signal_asset_flow_context(d.get("symbol"), flow_path)
    if not snapshot:
        return

    overlay = _derive_signal_asset_flow_overlay_summary(d, snapshot)
    summary = overlay.get("summary") if isinstance(overlay.get("summary"), dict) else {}
    if not summary:
        return

    d["asset_flow_context"] = copy.deepcopy(snapshot)
    d["asset_flow_summary"] = copy.deepcopy(summary)

    display_lines = overlay.get("display_lines") if isinstance(overlay.get("display_lines"), list) else []
    if display_lines:
        d["flow_overlay"] = {
            "display_lines": [str(line).strip() for line in display_lines if isinstance(line, str) and str(line).strip()][:2],
            "summary": copy.deepcopy(summary),
        }

    confidence_down_steps = int(overlay.get("confidence_down_steps") or 0)
    confidence_up_steps = int(overlay.get("confidence_up_steps") or 0)
    if confidence_down_steps > 0:
        _downgrade_confidence(d, confidence_down_steps)
    elif confidence_up_steps > 0:
        _upgrade_confidence(d, confidence_up_steps)

    for warning in overlay.get("warnings") or []:
        _append_unique_str(d, "warnings", str(warning))

    if bool(overlay.get("prefer_wait_confirm")) and not bool(d.get("no_trade")):
        d["entry_mode"] = "wait_confirm"


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
ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL))
ap.add_argument("--params", default="params.json")
ap.add_argument("--analysis-prompt", default="prompt_analysis.txt")
ap.add_argument("--symbol", default=None, help="Например: ETH/USDT (single-режим)")
ap.add_argument(
    "--multi", action="store_true", help="Мульти-анализ по пулу (анализ + JSON в конце)"
)
args = ap.parse_args()

# ================= MULTI MODE =================
if args.multi:
    system_prompt = read_file(args.analysis_prompt)
    time_str = current_msk()
    analysis_profile = resolve_event_calendar_profile(system_prompt=system_prompt)

    params_payload = {}
    multi_hints = {}
    if os.path.exists(args.params):
        try:
            with open(args.params, "r", encoding="utf-8") as f:
                params_payload = json.load(f)
                multi_hints = params_payload.get("hints", {}) or {}
        except Exception:
            multi_hints = {}
    force_mode = os.getenv("FORCE_MODE")
    mode_raw = force_mode if (isinstance(force_mode, str) and force_mode.strip()) else multi_hints.get("mode")
    mode = normalize_mode(mode_raw)
    requested_mode = normalize_mode(mode_raw) if (mode_raw is not None and str(mode_raw).strip()) else None

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
    calendar_context = load_event_calendar_context(analysis_profile, now_msk=time_msk)

    day_txt = read_latest_report_text("day", limit_chars=2000)
    mid_txt = read_latest_report_text("mid", limit_chars=2000)
    day_report_payload = read_latest_report_payload("day", max_age_hours=36)
    mid_report_payload = read_latest_report_payload("mid", max_age_hours=120)
    local_event_risk_snapshot = build_event_risk_context(
        news_block,
        calendar_events=calendar_context.get("calendar_events") or [],
    )
    aia_event_risk_context = read_aia_event_risk_context()
    flow_derivatives_context = (
        read_aia_flow_derivatives_context()
        if analysis_profile == "day"
        else read_mid_aia_flow_derivatives_context()
        if analysis_profile == "mid"
        else {}
    )

    pool_symbols = sorted(pool_snapshot.keys())
    pool_technical_context = build_pool_prompt_technical_context(pool_snapshot)
    pool_payload = {
        "time_msk_hint": time_msk,
        "snapshot_text": snapshot,
        "pool_symbols": pool_symbols,
        "pool_quotes": pool_snapshot,
        "pool_technical_context": pool_technical_context,
        "btc_eth_change": {
            "btc_change_pct": btc_info.get("change"),
            "eth_change_pct": eth_info.get("change"),
        },
        "mode": mode,
        "event_risk_context": local_event_risk_snapshot,
        "aia_event_risk_context": aia_event_risk_context,
        "calendar_generated_at_utc": calendar_context.get("generated_at_utc"),
        "calendar_events": calendar_context.get("calendar_events") or [],
    }
    if flow_derivatives_context:
        pool_payload["flow_market_context"] = flow_derivatives_context
    if requested_mode is not None:
        pool_payload["requested_mode"] = requested_mode

    if analysis_profile == "mid":
        overview_contract = (
            "overview: 5–6 коротких strategic paragraphs, каждый отдельной строкой массива, в таком порядке: "
            "1) `📰 MID • <time>` + `1️⃣ Среднесрочный режим 3–7 дней`; "
            "2) `2️⃣ Главные драйверы`; "
            "3) `3️⃣ Flow / Derivatives` (compact, explicitly state when it is derivatives-only and not full flow confirmation); "
            "4) `4️⃣ Карта активов`; "
            "5) `5️⃣ Сценарии 3–7 дней`; "
            "6) `6️⃣ Практический вывод`.\n"
            "MID = strategic 3–7 day view, не intraday execution brief. Не дублируй одни и те же risk phrases в каждом абзаце: "
            "`no chase`, `confirmation required`, `headline risk` и macro windows упоминай один раз в regime/practical section, "
            "а не повторяй у каждого актива.\n"
        )
    else:
        overview_contract = (
            "overview: 5 коротких tactical paragraphs, каждый отдельной строкой массива, в таком порядке: "
            "1) `🗓 DAY • <time>` + `1️⃣ Режим дня`; "
            "2) `2️⃣ Кандидаты на сегодня`; "
            "3) `3️⃣ Execution plan`; "
            "4) `4️⃣ Что НЕ делать сегодня`; "
            "5) `5️⃣ События / риски сегодня`.\n"
            "DAY = tactical current-day view, не full 3–7 day strategic essay. PREV_MID используй как background, "
            "но не повторяй весь недельный план. Повторяющиеся risk phrases держи в regime/execution summary, "
            "а не размазывай по каждому кандидату.\n"
        )

    schema_hint = (
        risk_mode_hint
        + "\n=== JSON OUTPUT FORMAT (STRICT) ===\n"
        + "Верни ОДИН JSON c двумя ключами: overview (list of paragraphs) и signal (объект).\n"
        + overview_contract
        + "signal: объект с полями time_msk, symbol, price, direction, entry_range, sl, tp1, tp2, rr, "
        "take_profit_rules, break_even_rule, multi_tf_view, why_asset, news_context, upcoming_events, "
        "macro_risk_summary, event_risk_context, event_risk_context_timestamp_utc, market_context, "
        "validity_minutes, cancel_condition, technical_rationale, disclaimer, entry_mode, confidence, holding_horizon, "
        "confirmation_rules, alt_entry_range, entries, no_trade, no_trade_reasons, no_trade_hint, "
        "max_valid_minutes, ema20_m15, ema20_h1, ema_guard, day_mid_context, adx_guard, "
        "tp_by_mode, rr_by_mode, exit_plan_by_mode.\n"
        "upcoming_events: массив объектов будущих событий. Разрешены частично заполненные объекты; не опускай поле.\n"
        "macro_risk_summary: краткая строка с ближайшими risk windows; если риска нет — пустая строка.\n"
        "event_risk_context: отдельный structured layer для unscheduled / developing catalysts. "
        "Не смешивай его с scheduled calendar_events или upcoming_events.\n"
        "event_risk_context_timestamp_utc: timestamp построения structured catalyst layer.\n"
        "news_context: выбери 1–3 строки ПРЯМО из блока [NEWS] или NEWS_FOCUS и вставь их БЕЗ ИЗМЕНЕНИЙ, "
        "сохраняя префикс времени вида \"[YYYY-MM-DD HH:MM МСК]\" и тег [impact:…]. Нельзя удалять/менять "
        "timestamp/impact или переписывать заголовок. Допускается добавить пояснение только в конце через "
        "\" — ...\" после исходной строки.\n"
        "Для entry_range / SL / TP / R:R используй только уровни, которые уже есть в snapshot_text, pool_quotes "
        "или pool_technical_context. Если используешь EMA/structure anchors, бери их из pool_technical_context, "
        "а не требуй их повторно у пользователя.\n"
        "holding_horizon: один из `intraday`, `intraday_to_1_2d`, `short_swing`, `multi_day`.\n"
        "Для aggressive / wait_confirm не пиши в why_asset или technical_rationale фразы про горизонт `3–7 дней`; "
        "используй wording уровня `тактический сетап intraday / 1–2 дня` или `short swing`.\n"
        "В why_asset не используй отрицательные сравнения против активов с H1 выше EMA60: "
        "это признак силы, а не слабости. Для негативного сравнения используй формулировки типа "
        "\"смешанная H1-структура\", \"ниже EMA60\" или \"хуже формализуется риск\".\n"
        f"symbol выбирай ТОЛЬКО из списка: {', '.join(pool_symbols)}.\n"
        f"time_msk установи ровно в это значение: {time_msk}.\n"
        "market_context ссылайся на проценты из блока [BTC_ETH_24H] как есть.\n"
        "event_risk_context — это локальный analysis-layer для headline catalysts: используй его как explanatory/advisory context "
        "и держи ОТДЕЛЬНО от scheduled calendar_events.\n"
        "aia_event_risk_context используй только как soft bias: он может смещать entry_type к wait_confirm, "
        "снижать агрессивность и требовать более аккуратного исполнения вокруг окна события, но не должен сам по себе жёстко запрещать сделку.\n"
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
        "Сначала восстанови официальный mode target ladder: TP1 около 1% чистого движения от entry, "
        "TP2 около 2%, TP3 optional/extended. Ближайший micro-level/recent high-low можно упомянуть как "
        "reaction level, но не делай его официальным TP1, если он слишком близко. "
        "Только если после восстановления mode ladder невозможно собрать валидный контракт — верни no_trade=true.\n"
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
        + f"MODE: {mode}. Follow the MODE-SPECIFIC DECISION CONTRACT below.\n"
        + json.dumps(pool_payload, ensure_ascii=False, indent=2)
        + "\n"
        + news_block
    )
    user_prompt += build_event_calendar_prompt_block(
        analysis_profile,
        now_msk=time_msk,
        calendar_context=calendar_context,
    )
    if os.getenv("TRACE_LLM_INPUT") == "1":
        print(
            f"\n=== [LLM MODE LINE] ===\nMODE: {mode}. Follow the MODE-SPECIFIC DECISION CONTRACT below.\n"
        )
        print(
            "\n=== [LLM PAYLOAD JSON] ===\n"
            + json.dumps(pool_payload, ensure_ascii=False, indent=2)
            + "\n"
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
    if day_report_payload or mid_report_payload:
        user_prompt += (
            "\n=== DAY/MID STRUCTURED FORWARD RISK ===\n"
            "Ниже — структурированный календарь риска из последних DAY/MID JSON. Используй его как risk modifier.\n"
            + json.dumps(
                {
                    "day": day_report_payload or {},
                    "mid": mid_report_payload or {},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    user_prompt += (
        "\n=== EVENT-RISK CONTEXT (LOCAL, SEPARATE FROM SCHEDULED CALENDAR) ===\n"
        "Это локальный structured layer по unscheduled / developing catalysts из headline inputs. "
        "Scheduled calendar_events НЕ смешивай с этим блоком. Если regime_layer по geopolitics = high/severe, "
        "считай его сильнее пустого calendar и сильнее supportive flow overlay для regime framing.\n"
        + json.dumps(local_event_risk_snapshot, ensure_ascii=False, indent=2)
        + "\n"
    )
    user_prompt += (
        "\n=== AIA EVENT RISK CONTEXT ===\n"
        "Это отдельный soft-bias слой от AIA. Не превращай его в hard-block: используй для bias к wait_confirm, "
        "осторожности в aggressive mode и более аккуратного выбора execution вокруг event windows.\n"
        + json.dumps(aia_event_risk_context, ensure_ascii=False, indent=2)
        + "\n"
    )
    if analysis_profile == "day" and flow_derivatives_context:
        user_prompt += build_flow_derivatives_prompt_block(flow_derivatives_context)
    elif analysis_profile == "mid" and flow_derivatives_context:
        user_prompt += build_flow_derivatives_prompt_block(flow_derivatives_context, analysis_profile="mid")

    resp = client.chat.completions.create(
        model=args.model,
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

    merge_event_calendar_context(
        signal_raw,
        profile=analysis_profile,
        now_msk=time_msk,
        calendar_context=calendar_context,
    )
    merge_day_mid_report_context(
        signal_raw,
        day_report=day_report_payload,
        mid_report=mid_report_payload,
    )
    attach_event_risk_context(signal_raw, local_event_risk_snapshot)
    raw_ctx = signal_raw.get("day_mid_context") if isinstance(signal_raw.get("day_mid_context"), dict) else {}
    raw_ctx = dict(raw_ctx)
    attach_event_risk_context(
        raw_ctx,
        local_event_risk_snapshot,
        field_name="event_risk_context",
        timestamp_field="event_risk_context_timestamp_utc",
    )
    signal_raw["day_mid_context"] = raw_ctx

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
    merge_event_calendar_context(
        signal,
        profile=analysis_profile,
        now_msk=time_msk,
        calendar_context=calendar_context,
    )
    attach_event_risk_context(signal, local_event_risk_snapshot)
    final_ctx = signal.get("day_mid_context") if isinstance(signal.get("day_mid_context"), dict) else {}
    final_ctx = dict(final_ctx)
    attach_event_risk_context(
        final_ctx,
        local_event_risk_snapshot,
        field_name="event_risk_context",
        timestamp_field="event_risk_context_timestamp_utc",
    )
    signal["day_mid_context"] = final_ctx

    _debug_trace_set_entry_range("entry_range_post_finalize", signal)
    _debug_trace_set_entry_range("entry_range_final", signal)
    _debug_trace_write()

    if overview_lines:
        flow_derivatives_section = ""
        if analysis_profile == "day":
            flow_derivatives_section = render_flow_derivatives_context_section(
                read_aia_flow_derivatives_context(),
                signal_payload=signal,
                detail_level="day_compact",
            )
        elif analysis_profile == "mid":
            flow_derivatives_section = render_flow_derivatives_context_section(
                flow_derivatives_context,
                signal_payload=signal,
                detail_level="compact",
            )

        print("\n=== [POOL OVERVIEW] ===\n")
        for idx, section in enumerate(
            _compose_overview_sections(
                overview_lines,
                analysis_profile=analysis_profile,
                flow_derivatives_section=flow_derivatives_section,
            )
        ):
            if idx:
                print()
                print()
            print(section)

        if analysis_profile in {"day", "mid"}:
            print()
            print(
                render_calendar_section(
                    calendar_context.get("calendar_events") or [],
                    empty_message=_calendar_empty_message_for_event_risk(local_event_risk_snapshot),
                )
            )
            event_risk_section = render_event_risk_context_section(
                local_event_risk_snapshot,
                profile=analysis_profile,
            )
            if event_risk_section:
                print()
                print(event_risk_section)
            if analysis_profile == "day" and flow_derivatives_section:
                print()
                print(flow_derivatives_section)
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
payload["hints"]["event_risk_context"] = read_aia_event_risk_context()
signal_calendar_context = load_event_calendar_context("signal", now_msk=payload["hints"]["time_msk"])

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
force_mode = os.getenv("FORCE_MODE")
mode_raw = force_mode if (isinstance(force_mode, str) and force_mode.strip()) else payload["hints"].get("mode")
mode = normalize_mode(mode_raw)
payload["hints"]["mode"] = mode
payload["mode"] = mode
if "requested_mode" not in payload and mode_raw is not None and str(mode_raw).strip():
    payload["requested_mode"] = normalize_mode(mode_raw)
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
    "Входные данные:\n"
    + f"MODE: {mode}. Follow the MODE-SPECIFIC DECISION CONTRACT below.\n"
    + json.dumps(payload, ensure_ascii=False)
)
if os.getenv("TRACE_LLM_INPUT") == "1":
    print(
        f"\n=== [LLM MODE LINE] ===\nMODE: {mode}. Follow the MODE-SPECIFIC DECISION CONTRACT below.\n"
    )
    print("\n=== [LLM PAYLOAD JSON] ===\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

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
    "  Не называй активы с H1 выше EMA60 причиной, почему они хуже; негативное сравнение допустимо только для "
    "mixed H1 / ниже EMA60 / хуже формализуемого риска.\n"
    "- news_context: массив строк из блока [NEWS]/NEWS_FOCUS (уже с префиксом даты и impact), без изменения текста\n"
    "- upcoming_events: массив объектов ближайших событий; если событий нет — []\n"
    "- macro_risk_summary: строка с ближайшими risk windows; если их нет — пустая строка\n"
    "- market_context: строка с использованием [BTC_ETH_24H]\n"
    "- validity_minutes: число (например, 90)\n"
    "- cancel_condition: строка — при каких условиях сетап отменяется\n"
    "- technical_rationale: строка — связное обоснование сетапа (можно длинное)\n"
    "- disclaimer: строка с дисклеймером\n"
    "- entry_mode: 'limit', 'now' или 'wait_confirm'\n"
    "- confidence: 'High' | 'Medium' | 'Low'\n"
    "- holding_horizon: 'intraday' | 'intraday_to_1_2d' | 'short_swing' | 'multi_day'\n"
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
    "- hints.event_risk_context: soft bias от AIA; используй его для более осторожного выбора entry_type / aggressiveness / execution around event windows, но не делай из него hard-block\n"
    "Для aggressive / wait_confirm не используй фразы вроде `горизонт 3–7 дней` в why_asset или technical_rationale; "
    "это wording для MID view, а не для тактического сигнала.\n"
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
    "Сначала восстанови официальный mode target ladder: TP1 около 1% чистого движения от entry, "
    "TP2 около 2%, TP3 optional/extended. Ближайший micro-level/recent high-low можно описать как "
    "reaction level, но не делай его официальным TP1, если он слишком близко. "
    "Только если после восстановления mode ladder нельзя получить валидный контракт — верни no_trade=true.\n"
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
day_report_payload = read_latest_report_payload("day", max_age_hours=36)
mid_report_payload = read_latest_report_payload("mid", max_age_hours=120)
event_risk_context = read_aia_event_risk_context()
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
if day_report_payload or mid_report_payload:
    user_prompt += (
        "\n=== DAY/MID STRUCTURED FORWARD RISK ===\n"
        "Ниже — структурированный календарь риска из последних DAY/MID JSON. Используй его как risk modifier.\n"
        + json.dumps(
            {
                "day": day_report_payload or {},
                "mid": mid_report_payload or {},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
user_prompt += (
    "\n=== AIA EVENT RISK CONTEXT ===\n"
    "Это отдельный soft-bias слой от AIA. Не делай из него hard-block: используй его для bias к wait_confirm, "
    "меньшей агрессии и более аккуратного execution вокруг event windows.\n"
    + json.dumps(event_risk_context, ensure_ascii=False, indent=2)
    + "\n"
)

# === LLM вызов (SINGLE) ===
resp = client.chat.completions.create(
    model=args.model,
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

merge_event_calendar_context(
    data,
    profile="signal",
    now_msk=payload.get("hints", {}).get("time_msk"),
    calendar_context=signal_calendar_context,
)
merge_day_mid_report_context(
    data,
    day_report=day_report_payload,
    mid_report=mid_report_payload,
)

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
merge_event_calendar_context(
    data,
    profile="signal",
    now_msk=payload.get("hints", {}).get("time_msk"),
    calendar_context=signal_calendar_context,
)

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
