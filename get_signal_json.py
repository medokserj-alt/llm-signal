#!/usr/bin/env python3
import os, sys, json, argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from openai import OpenAI
import ccxt
import subprocess
from pathlib import Path

# ---- EMA20(M15) helpers ----
def _ema(vals, period=20):
    if not vals or len(vals) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = float(vals[-period])
    for v in vals[-period+1:]:
        ema = float(v) * k + ema * (1.0 - k)
    return round(ema, 6)

def get_ema20_m15(symbol: str):
    for ex in (ccxt.bybit(), ccxt.binance()):
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe='15m', limit=25)
            closes = [c[4] for c in ohlcv]
            e = _ema(closes, 20)
            if e:
                return float(e)
        except Exception:
            pass
    return None

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

# --- LESSONS loader (runtime, robust) ---
import pathlib as _pl

def _normalize_side(d: dict) -> None:
    raw = (d.get("side") or d.get("direction") or "").strip().lower()
    m = {"buy":"long","sell":"short","long":"long","short":"short","l":"long","s":"short"}
    if raw in m:
        d["side"] = m[raw]
        return
    tr = (d.get("technical_rationale") or "").lower()
    if " short" in tr and " long" not in tr:
        d["side"] = "short"
    elif " long" in tr and " short" not in tr:
        d["side"] = "long"

def _normalize_entry_mode(d: dict) -> None:
    em = (d.get("entry_mode") or "").strip().lower()
    if em in {"market","now","mkt"}:
        flags = [f.lower() for f in (d.get("risk_flags") or [])]
        overbought_hint = any(k in (d.get("technical_rationale","").lower()) for k in ["перекуп", "overbought"])
        weak_htf = any("weak_htf_rsi" in f or "htf" in f for f in flags)
        high_conf = (d.get("confidence") == "High")
        if high_conf and not (overbought_hint or weak_htf):
            d["entry_mode"] = "now"
            d.setdefault("warnings", []).append("market_entry_high_conf")
        else:
            d["entry_mode"] = "limit"
            d.setdefault("warnings", []).append("market_downgraded_to_limit")
    elif em in {"wait_confirm","wait-confirm","confirm","wc"}:
        d["entry_mode"] = "wait_confirm"
    elif em in {"limit","lim","lmt"}:
        d["entry_mode"] = "limit"

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
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    body = "\n".join(lines).strip()
    if not body:
        return ("", 0, str(q))
    text = "\n# [LESSONS]\n" + body + "\n"
    try:
        if "AUTO_LESSONS" not in raw:
            _ap = (_pl.Path(__file__).resolve().parent / "auto_feedback/lessons/Auto_Lessons.md").resolve()
            if _ap.exists():
                _auto = _ap.read_text(encoding="utf-8").strip()
                if _auto:
                    body2 = (body + "\n" + _auto).strip()
                    text  = "\n# [LESSONS]\n" + body2 + "\n"
                    lines = [ln for ln in body2.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
                    return (text, len(lines), str(q))
    except Exception:
        pass
    return (text, len(lines), str(q))

# ---------------- Helpers ----------------
def snapshot_from_status() -> str:
    """Возвращаем текст от ./status (или ./status --for-llm), без падений при ошибке."""
    try:
        cmd = 'cd ~/llm-signal && ./status --for-llm 2>/dev/null || ./status 2>/dev/null'
        res = subprocess.run(["bash","-lc",cmd], capture_output=True, text=True, timeout=30)
        return res.stdout.strip()
    except Exception:
        return ""

def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def current_msk() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")

# ---- тикеры с last и 24h % (на всякий) ----
def _fetch_ticker(ex, sym):
    t = ex.fetch_ticker(sym)
    last = t.get("last")
    if last is None:
        bid, ask = t.get("bid"), t.get("ask")
        if bid and ask:
            last = (bid + ask) / 2
    change = t.get("percentage")  # 24h %
    return float(last) if last is not None else None, (float(change) if change is not None else None)

def get_pair_ticker(sym: str):
    bybit = ccxt.bybit(); binance = ccxt.binance()
    for ex in (bybit, binance):
        try:
            last, change = _fetch_ticker(ex, sym)
            if last is not None:
                return {"last": last, "change": change}
        except Exception:
            pass
    return {"last": None, "change": None}

def get_pool_snapshot() -> dict:
    with open("pool.json","r",encoding="utf-8") as f:
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
            ["bash","-lc",f"./news_snapshot.py {hours}"],
            capture_output=True, text=True, timeout=30
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
    lines = [ln.strip("- ").strip() for ln in news_block.splitlines() if ln.strip().startswith("- ")]
    picked = []
    for ln in lines:
        u = ln.upper()
        if (symbol_root and symbol_root in u) or any(k in u for k in ("ETF","SEC","FED","MACRO","INFLATION","LIQUIDATION","LIQUIDATIONS")):
            picked.append(ln)
        if len(picked) >= 3:
            break
    return "\n".join(picked)

# ---------------- Bootstrap ----------------
BASE = Path(__file__).resolve().parent
load_dotenv(BASE/".env")
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("ERROR: OPENAI_API_KEY not set (put it in .env)", file=sys.stderr)
    sys.exit(1)
client = OpenAI(api_key=api_key)

ap = argparse.ArgumentParser()
ap.add_argument("--model", default=os.getenv("OPENAI_MODEL","gpt-4.1-mini"))
ap.add_argument("--params", default="params.json")
ap.add_argument("--symbol", default=None, help="Например: ETH/USDT (single-режим)")
ap.add_argument("--multi", action="store_true", help="Мульти-анализ по пулу (анализ + JSON в конце)")
args = ap.parse_args()

# ================= MULTI MODE =================
if args.multi:
    system_prompt = read_file("prompt_analysis.txt")

    snapshot = snapshot_from_status()
    if not snapshot:
        btc  = get_pair_ticker("BTC/USDT")
        eth  = get_pair_ticker("ETH/USDT")
        pool = get_pool_snapshot()
        time_str = current_msk()
        ctx = []
        ctx.append(f"Время (МСК): {time_str}")
        ctx.append("Контекст BTC/ETH (используй ТОЛЬКО эти значения; числовые уровни/диапазоны не придумывать):")
        ctx.append(f"BTC/USDT: last={btc['last']}, change_24h={btc['change']}%")
        ctx.append(f"ETH/USDT: last={eth['last']}, change_24h={eth['change']}%")
        ctx.append("")
        ctx.append("Котировки альт-пула (USDT, 24h %):")
        for k, v in pool.items():
            ctx.append(f"{k}: last={v['last']}, change_24h={v['change']}%")
        snapshot = "\n".join(ctx)

    btc_info = get_pair_ticker("BTC/USDT")
    eth_info = get_pair_ticker("ETH/USDT")
    btc_eth_line = (
        f"\n[BTC_ETH_24H]\n"
        f"BTC change_24h={btc_info['change']}%, ETH change_24h={eth_info['change']}% "
        f"(используй РОВНО эти проценты в поле market_context)\n"
    )

    try:
        with open("snapshot.txt", "w", encoding="utf-8") as f:
            f.write(snapshot + "\n")
    except Exception:
        pass
    print("\n=== [SNAPSHOT ДЛЯ LLM] ===\n" + snapshot + "\n==========================\n")

    news_block = get_news_block(12)
    lessons_text, lessons_count, lessons_path = ("", 0, "")

    user_prompt = (
        ((lessons_text + "\n") if lessons_text else "")
        + snapshot + btc_eth_line + "\n"
        + "Требования к описанию BTC/ETH: опирайся только на last и change_24h; не указывай числовые уровни/диапазоны, которых нет во входе. "
        + "Оцени общий фон (risk-on/нейтрально/risk-off) исходя из снапшота. "
        + "Сначала выдай свободный аналитический обзор по пулу (4–6 абзацев). "
        + "В конце — чистый JSON-блок сигнала по указанной схеме (без текста вокруг)."
        + news_block
    )

    user_prompt += (
        "\n\n=== OUTPUT STYLE REQUIREMENTS ===\n"
        "- symbol: строго в формате TICKER/USDT из пула (например, LINK/USDT; НЕ LINKUSDT).\n"
        "- time_msk: формат ровно 'dd.mm.yyyy, HH:MM' по МСК.\n"
        "- news_context: выдай 1–3 пункта из [NEWS]/NEWS_FOCUS. Каждый пункт в формате: "
        "\"- [impact:+/−/neutral] краткий заголовок — зачем это важно для выбранного актива (≤15 слов)\". "
        "Используй только факты из [NEWS], не придумывай уровни/цифры.\n"
        "- market_context: используй блок [BTC_ETH_24H] с процента́ми КАК ЕСТЬ.\n"
    )

    focus = build_news_focus("", news_block)
    if focus:
        user_prompt += "\nNEWS_FOCUS (top-3):\n" + focus + "\n"

    resp = client.chat.completions.create(
        model="gpt-5.1",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.85, top_p=0.95, seed=7,
    )
    print(resp.choices[0].message.content)
    sys.exit(0)

# ================= SINGLE MODE =================
system_prompt = read_file("prompt_system.txt")
anna_prompt   = read_file("prompt_anna.txt")

payload = {}
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
            _val = float(_last)
            _prec = 4 if _val < 1 else (3 if _val < 10 else 2)
            payload["hints"]["price"] = round(_val, _prec)
            payload["hints"]["price_source"] = "live"
        except Exception:
            pass

# time hint
payload["hints"]["time_msk"] = current_msk()

# EMA20(M15) для выбранного символа (если есть)
_sym = payload["hints"].get("symbol")
payload["hints"]["ema20_m15"] = get_ema20_m15(_sym) if _sym else None

# базовый user_prompt
base_user_prompt = (
    "Сгенерируй один JSON по заданной схеме. "
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

# финальный user_prompt для SINGLE
user_prompt = (
    ((lessons_text + "\n") if lessons_text else "")
    + base_user_prompt
    + news_block
)

# стиль + формат symbol/time_msk
user_prompt += (
    "\n\n=== OUTPUT STYLE REQUIREMENTS ===\n"
    "- symbol: строго в формате TICKER/USDT из пула (например, LINK/USDT; НЕ LINKUSDT).\n"
    "- time_msk: формат ровно 'dd.mm.yyyy, HH:MM' по МСК.\n"
    "- news_context: выдай 1–3 пункта из [NEWS]/NEWS_FOCUS. Каждый пункт в формате: "
    "\"- [impact:+/−/neutral] краткий заголовок — зачем это важно для выбранного актива (≤15 слов)\". "
    "Используй только факты из [NEWS], не придумывай уровни/цифры.\n"
)

# NEWS_FOCUS c учётом тикера
focus = build_news_focus(payload.get("hints", {}).get("symbol", ""), news_block)
if focus:
    user_prompt += "\nNEWS_FOCUS (top-3):\n" + focus + "\n"

# === LLM вызов (SINGLE) ===
resp = client.chat.completions.create(
    model="gpt-5.1",
    response_format={"type": "json_object"},
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": anna_prompt},
        {"role": "user",   "content": user_prompt},
    ],
    temperature=0.4,
    top_p=0.85
)
content = resp.choices[0].message.content

# Разбор JSON
try:
    data = json.loads(content)

    # lift ema20_m15 из hints (или досчитать при необходимости)
    try:
        if 'ema20_m15' not in data or not data['ema20_m15']:
            sym_for_ema = payload.get('hints', {}).get('symbol') or data.get('symbol')
            data['ema20_m15'] = payload.get('hints', {}).get('ema20_m15') or (get_ema20_m15(sym_for_ema) if sym_for_ema else None)
    except Exception:
        pass

except Exception:
    print(content)
    sys.exit(0)

# Переписываем ключи из hints (гарантии)
_h = payload.get("hints", {})
if _h.get("symbol"):
    data["symbol"] = _h["symbol"]
if _h.get("time_msk"):
    data["time_msk"] = _h["time_msk"]
if "price" in _h and _h["price"] is not None:
    data["price"] = _h["price"]

# Нормализация SINGLE
def _normalize_side_local(d: dict) -> None:
    raw = (d.get("side") or d.get("direction") or "").strip().lower()
    m = {"buy": "long", "sell": "short", "long": "long", "short": "short", "l":"long", "s":"short"}
    if raw in m:
        d["side"] = m[raw]; return
    tr = (d.get("technical_rationale") or "").lower()
    if " short" in tr and " long" not in tr:
        d["side"] = "short"
    elif " long" in tr and " short" not in tr:
        d["side"] = "long"
    else:
        d.setdefault("side", "long")

def _normalize_entry_mode_local(d: dict) -> None:
    em = (d.get("entry_mode") or "").strip().lower()
    if em in {"market", "now", "mkt"}:
        flags = [f.lower() for f in (d.get("risk_flags") or [])]
        overbought_hint = any(k in (d.get("technical_rationale","").lower()) for k in ["перекуп","overbought"])
        weak_htf = any("weak_htf_rsi" in f or "htf" in f for f in flags)
        high_conf = (d.get("confidence") == "High")
        if high_conf and not (overbought_hint or weak_htf):
            d["entry_mode"] = "now"
            d.setdefault("warnings", []).append("market_entry_high_conf")
        else:
            d["entry_mode"] = "limit"
            d.setdefault("warnings", []).append("market_downgraded_to_limit")
    elif em in {"wait_confirm","wait-confirm","confirm","wc"}:
        d["entry_mode"] = "wait_confirm"
    elif em in {"limit","lim","lmt"}:
        d["entry_mode"] = "limit"
    else:
        d.setdefault("entry_mode", "limit")

_normalize_side_local(data)
_normalize_entry_mode_local(data)

print(json.dumps(data, ensure_ascii=False))
sys.exit(0)
