#!/usr/bin/env python3
import sys
import json
import re
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo


def fmt(x):
    try:
        v = float(x)
    except Exception:
        return str(x)
    # до 6 знаков, затем сжать "лишние" нули и точку
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    # для очень мелких цен типа 0.00000725 не резать значащие нули
    if "e-" in f"{v:.2e}":
        s = f"{v:.8f}".rstrip("0").rstrip(".")
    return s


def trim_all_numbers(s: str) -> str:
    # Уже сформированный текст: убрать .000000 → пусто, .120000 → .12
    def _fix(m):
        whole = m.group(1)  # '184.210000' или '192.000000'
        core = re.sub(r"0+$", "", whole)  # '184.21' или '192.'
        return core.rstrip(".")  # '192'

    return re.sub(r"(\d+\.\d{2,})0+\b", _fix, s)


def now_msk() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")

def _drop_time_window_mentions(data: dict) -> dict:
    def has_tw(s: str) -> bool:
        return "time_window" in s

    def walk(x):
        if isinstance(x, dict):
            for k in list(x.keys()):
                v = x[k]
                if k == "no_trade_reasons" and isinstance(v, list):
                    x[k] = [it for it in v if not (isinstance(it, str) and has_tw(it))]
                    continue
                if k == "warnings" and isinstance(v, list):
                    x[k] = [it for it in v if not (isinstance(it, str) and has_tw(it))]
                    continue
                if k in ("no_trade_hint", "comments") and isinstance(v, str) and has_tw(v):
                    x[k] = ""
                    continue
                if k == "comment" and isinstance(v, str) and has_tw(v):
                    x[k] = ""
                    continue
                walk(v)
        elif isinstance(x, list):
            for it in x:
                walk(it)

    try:
        walk(data)
    except Exception:
        pass
    return data

def _truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if not s or len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut.rstrip(".,;:—- ") + "…"

def _midpoint_price(r: dict) -> str:
    mn, mx = (r or {}).get("min"), (r or {}).get("max")
    if mn is None or mx is None:
        return "—"
    try:
        return fmt((float(mn) + float(mx)) / 2.0)
    except Exception:
        return "—"


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERR: empty stdin", file=sys.stderr)
        sys.exit(1)
    data = _drop_time_window_mentions(json.loads(raw))

    # базовые поля
    time_msk = data.get("time_msk", now_msk())
    symbol = data.get("symbol", "?")
    price = fmt(data.get("price", ""))

    direction = (data.get("direction") or "").lower() or "—"

    sl = fmt(data.get("sl", ""))
    tp1 = fmt(data.get("tp1", ""))
    tp2 = fmt(data.get("tp2", ""))
    rr = data.get("rr", None)
    rr_str = f"1:{fmt(rr)}" if rr is not None else "—"

    take_profit_rules = (data.get("take_profit_rules") or "").strip()
    break_even_rule = (data.get("break_even_rule") or "").strip()
    mtf = data.get("multi_tf_view", {}) or {}
    why_asset = (data.get("why_asset") or "").strip()
    news_ctx = data.get("news_context", []) or []
    market_ctx = (data.get("market_context") or "").strip()
    raw_tr = data.get("technical_rationale") or ""
    if isinstance(raw_tr, dict):
        rationale = (raw_tr.get("summary") or "").strip()
    elif isinstance(raw_tr, str):
        rationale = raw_tr.strip()
    else:
        rationale = str(raw_tr).strip()
    disclaimer = (
        (data.get("disclaimer") or "").strip()
        or "Не является инвестиционной рекомендацией. DYOR."
    )

    # новые поля: entries + no_trade
    entries = data.get("entries") or {}
    no_trade = bool(data.get("no_trade"))
    no_trade_reasons = data.get("no_trade_reasons") or []
    no_trade_hint = (data.get("no_trade_hint") or "").strip()

    # --- формируем текст (новый формат) ---
    lines: list[str] = []

    lines.append(f"🕗 Время (МСК): {time_msk}  💰 Текущая цена: {price}")
    if not no_trade:
        lines.append(f"📊 Актив: {symbol}  📈 Направление: {direction}")
    else:
        lines.append(f"📊 Актив: {symbol}")
    lines.append("")

    # Цена входа (Aggressive/Neutral/Conservative) (по entry_price_*; fallback на midpoint)
    agg = entries.get("aggressive") or {}
    neu = entries.get("neutral") or {}
    con = entries.get("conservative") or {}

    entry_price_aggressive = (
        fmt(data.get("entry_price_aggressive"))
        if data.get("entry_price_aggressive") is not None
        else _midpoint_price(agg.get("range") or {})
    )
    entry_price_neutral = (
        fmt(data.get("entry_price_neutral"))
        if data.get("entry_price_neutral") is not None
        else _midpoint_price(neu.get("range") or data.get("entry_range") or {})
    )
    entry_price_conservative = (
        fmt(data.get("entry_price_conservative"))
        if data.get("entry_price_conservative") is not None
        else _midpoint_price(con.get("range") or {})
    )

    lines.append("🎯 Цена входа")
    lines.append(f"- Aggressive: {entry_price_aggressive if agg.get('enabled', True) else '—'}")
    lines.append(f"- Neutral: {entry_price_neutral if neu.get('enabled', True) else '—'}")
    lines.append(f"- Conservative: {entry_price_conservative if con.get('enabled', True) else '—'}")
    lines.append("")

    # TP как ТВХ (по режимам) + SL/RR (по sl_by_mode/tp_by_mode/rr_by_mode; fallback на единые)
    sl_by_mode = data.get("sl_by_mode") if isinstance(data.get("sl_by_mode"), dict) else None
    tp_by_mode = data.get("tp_by_mode") if isinstance(data.get("tp_by_mode"), dict) else None
    rr_by_mode = data.get("rr_by_mode") if isinstance(data.get("rr_by_mode"), dict) else None
    if sl_by_mode is None:
        sl_by_mode = {"aggressive": sl, "neutral": sl, "conservative": sl}
    if tp_by_mode is None:
        tp_by_mode = {
            "aggressive": {"tp1": tp1, "tp2": tp2},
            "neutral": {"tp1": tp1, "tp2": tp2},
            "conservative": {"tp1": tp1, "tp2": tp2},
        }
    if rr_by_mode is None:
        rr_by_mode = {"aggressive": rr_str, "neutral": rr_str, "conservative": rr_str}

    lines.append("🎯 ТВХ / SL / R:R (по режимам)")
    for mode in ("aggressive", "neutral", "conservative"):
        tvx = tp_by_mode.get(mode) or {}
        tvx_str = f"TP1 {fmt(tvx.get('tp1', tp1))}, TP2 {fmt(tvx.get('tp2', tp2))}"
        lines.append(
            f"- {mode}: SL {fmt(sl_by_mode.get(mode, sl))} • ТВХ {tvx_str} • R:R {rr_by_mode.get(mode, rr_str)}"
        )
    lines.append("")

    # План выхода (по режимам) (exit_plan_by_mode; fallback на общие правила)
    exit_plan_by_mode = (
        data.get("exit_plan_by_mode") if isinstance(data.get("exit_plan_by_mode"), dict) else None
    )
    if exit_plan_by_mode is None:
        plan = " ".join(x for x in [take_profit_rules, break_even_rule] if x).strip()
        exit_plan_by_mode = {"aggressive": plan, "neutral": plan, "conservative": plan}

    lines.append("🧾 План выхода (по режимам)")
    for mode in ("aggressive", "neutral", "conservative"):
        v = (exit_plan_by_mode.get(mode) or "").strip()
        lines.append(f"- {mode}: {v or '—'}")
    lines.append("")

    # Таймфреймы (одной строкой)
    lines.append("⏱ Таймфреймы")
    tf_order = ["m5", "m15", "h1", "h4", "d1"]
    tf_parts = []
    for k in tf_order:
        if k in mtf:
            label = (
                k.replace("m", "").replace("h", "").upper()
                if k != "d1"
                else "1D"
            )
            tf_parts.append(f"{label}: {mtf[k]}")
    if not tf_parts:
        tf_parts = [f"{k}: {v}" for k, v in mtf.items()]
    lines.append("; ".join(tf_parts) if tf_parts else "—")
    lines.append("")

    # Новостной фон (с таймстампом МСК)
    lines.append(f"📰 Новостной фон (МСК {time_msk})")
    if news_ctx:
        for it in news_ctx[:4]:
            lines.append(str(it))
    else:
        lines.append("Новостных триггеров не выявлено.")
    lines.append("")

    # Контекст рынка (коротко)
    lines.append("🌐 Контекст рынка")
    lines.append(_truncate(market_ctx or "—", 280) or "—")
    lines.append("")

    # Обоснование (коротко)
    lines.append("⚙️ Обоснование")
    merged_rationale = " ".join(x for x in [why_asset, rationale] if x).strip()
    lines.append(_truncate(merged_rationale or "—", 550) or "—")
    lines.append("")

    # Дисклеймер
    lines.append("⚠️ Дисклеймер")
    lines.append(disclaimer)

    text_out = trim_all_numbers("\n".join(lines))

    # HTML-пост (тот же контент, но без **)
    html = text_out.replace("**", "")
    # упрощённый html-конверт: \n -> <br>
    html = html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = html.replace("\n", "<br>\n")

    # сохранить HTML
    ts = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d_%H%M%S")
    out_name = f"signal_{ts}.html"
    pathlib.Path(out_name).write_text(html, encoding="utf-8")

    # вывод в stdout
    print(text_out)
    print(f"\n[OK] HTML сохранён: {out_name}")


if __name__ == "__main__":
    main()
