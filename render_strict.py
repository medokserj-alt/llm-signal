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


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERR: empty stdin", file=sys.stderr)
        sys.exit(1)
    data = json.loads(raw)

    # базовые поля
    time_msk = data.get("time_msk", now_msk())
    symbol = data.get("symbol", "?")
    price = fmt(data.get("price", ""))

    direction = (data.get("direction") or "").lower() or "—"

    er = data.get("entry_range", {}) or {}
    er_min, er_max = fmt(er.get("min", "")), fmt(er.get("max", ""))

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
    validity = data.get("validity_minutes", 90)
    cancel_cond = (data.get("cancel_condition") or "").strip()
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

    # --- формируем текст ---
    lines: list[str] = []

    # шапка
    lines.append(f"🕗 Время (МСК): {time_msk}  💰 Текущая цена: {price}")
    lines.append(f"📊 Актив: {symbol}")
    lines.append("")

    # 1. Почему выбран актив
    lines.append("1️⃣ Почему выбран актив")
    lines.append(why_asset or "—")
    lines.append("")

    # 2. Сетап
    lines.append("2️⃣ Сетап")

    if no_trade:
        # режим NO-TRADE: сигнал по системе не выдан
        lines.append("**Режим:** no-trade (сигнал по системе не выдан)")
    else:
        lines.append(f"**Направление:** {direction}")
        lines.append(f"**Диапазон входа (нейтральный):** {er_min}–{er_max}")
        lines.append(f"**SL:** {sl}")
        lines.append(f"**TP1:** {tp1}")
        lines.append(f"**TP2:** {tp2}")
        lines.append(f"**R:R:** {rr_str}")
        if take_profit_rules:
            lines.append(f"**Фиксация прибыли:** {take_profit_rules}")
        if break_even_rule:
            lines.append(f"**BE:** {break_even_rule}")

    # если есть entries — выводим три профиля
    if entries and not no_trade:
        aggr = entries.get("aggressive") or {}
        neutr = entries.get("neutral") or {}
        cons = entries.get("conservative") or {}

        def fmt_range(entry: dict) -> str:
            r = entry.get("range") or {}
            mn, mx = r.get("min"), r.get("max")
            if mn is None or mx is None:
                return "—"
            return f"{fmt(mn)}–{fmt(mx)}"

        lines.append("")
        lines.append("🎯 Точки входа")

        # Aggressive
        if aggr.get("enabled"):
            lines.append(
                f"• Агрессивный: {fmt_range(aggr)} "
                f"({aggr.get('position_size_hint','')}) — {aggr.get('comment','').strip()}"
            )

        # Neutral
        if neutr.get("enabled"):
            lines.append(
                f"• Нейтральный: {fmt_range(neutr)} "
                f"({neutr.get('position_size_hint','')}) — {neutr.get('comment','').strip()}"
            )

        # Conservative
        if cons.get("enabled") and not (
            cons.get("range", {}).get("min") in (0, None)
            and cons.get("range", {}).get("max") in (0, None)
        ):
            lines.append(
                f"• Консервативный: {fmt_range(cons)} "
                f"({cons.get('position_size_hint','')}) — {cons.get('comment','').strip()}"
            )

    # NO-TRADE пояснение
    if no_trade:
        lines.append("")
        lines.append("❌ Сигнал не выдан (NO-TRADE)")
        if no_trade_reasons:
            lines.append("Причины:")
            for r in no_trade_reasons:
                lines.append(f"- {str(r)}")
        if no_trade_hint:
            lines.append("")
            lines.append("Комментарий:")
            lines.append(no_trade_hint)

    lines.append("")

    # Market warning banner (старый блок оставляем)
    warnings = data.get("warnings") or []
    if "market_entry_high_conf" in warnings:
        lines.append(
            "⚠️ <b>Market entry</b>: высокая уверенность, но повышенный риск — "
            "используйте меньший размер позиции и ждите EMA подтверждения."
        )
    elif "market_downgraded_to_limit" in warnings:
        lines.append(
            "⚠️ <b>Market→Limit</b>: из-за перегрева или слабого HTF сигнал снижен до лимитного входа."
        )
    elif data.get("entry_mode") in ("market", "now") and not warnings:
        lines.append(
            "⚠️ <b>Market entry</b>: применяйте осторожность, контроль объёма обязателен."
        )

    if "time_window_low_liquidity" in warnings:
        lines.append(
            "⚠️ <b>Time-window</b>: рынок тонкий/волатильный, используйте консервативный вход и снижайте объём."
        )

    # 3. Картина по таймфреймам
    lines.append("3️⃣ Картина по таймфреймам")
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

    # 4. Новостной фон
    lines.append("4️⃣ Новостной фон")
    if news_ctx:
        for it in news_ctx[:4]:
            lines.append(str(it))
    else:
        lines.append("Новостных триггеров не выявлено.")
    lines.append("")

    # 5. Контекст рынка
    lines.append("5️⃣ Контекст рынка")
    lines.append(market_ctx or "—")
    lines.append("")

    # 6. Валидность сигнала
    lines.append("6️⃣ Валидность сигнала")
    lines.append(f"{validity} минут; {cancel_cond or '—'}")
    lines.append("")

    # Обоснование
    lines.append("⚙️ Обоснование")
    lines.append(rationale or "—")
    lines.append("")

    # Дисклеймер
    lines.append("⚠️ Дисклеймер")
    lines.append(disclaimer)

    # доп. market-entry предупреждение (как раньше)
    if (data.get("entry_mode") in ("market", "now")) or (
        "warnings" in data and "market_entry_high_conf" in data["warnings"]
    ):
        lines.append(
            "⚠️ <b>Market-entry</b>: повышенный риск; используйте сниженный размер позиции."
        )

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
