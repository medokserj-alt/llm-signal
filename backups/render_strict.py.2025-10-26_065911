#!/usr/bin/env python3
import sys, json, re, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

def fmt(x):
    try:
        v = float(x)
    except Exception:
        return str(x)
    # до 6 знаков, затем сжать "лишние" нули и точку
    s = f"{v:.6f}".rstrip('0').rstrip('.')
    # для мелких цен типа 0.00000725 не резать значащие нули
    if 'e-' in f"{v:.2e}":
        s = f"{v:.8f}".rstrip('0').rstrip('.')
    return s

def trim_all_numbers(s: str) -> str:
    # Уже сформированный текст: убрать .000000 → пусто, .120000 → .12
    def _fix(m):
        whole = m.group(1)         # "184.210000" или "192.000000"
        core  = re.sub(r'0+$','', whole)  # "184.21" или "192."
        return core.rstrip('.')            # "192"
    return re.sub(r'(\d+\.\d{2,})0+\b', _fix, s)

def now_msk():
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")

def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERR: empty stdin", file=sys.stderr); sys.exit(1)
    data = json.loads(raw)

    # поля
    time_msk           = data.get("time_msk", now_msk())
    symbol             = data.get("symbol","?")
    price              = fmt(data.get("price",""))
    direction          = data.get("direction","").lower()
    er                 = data.get("entry_range",{}) or {}
    er_min, er_max     = fmt(er.get("min","")), fmt(er.get("max",""))
    sl, tp1, tp2       = fmt(data.get("sl","")), fmt(data.get("tp1","")), fmt(data.get("tp2",""))
    rr                 = data.get("rr", None)
    rr_str             = f"1:{fmt(rr)}" if rr is not None else "—"
    take_profit_rules  = data.get("take_profit_rules","").strip()
    break_even_rule    = data.get("break_even_rule","").strip()
    mtf                = data.get("multi_tf_view",{}) or {}
    why_asset          = data.get("why_asset","").strip()
    news_ctx           = data.get("news_context",[]) or []
    market_ctx         = data.get("market_context","").strip()
    validity           = data.get("validity_minutes", 90)
    cancel_cond        = data.get("cancel_condition","").strip()
    rationale          = data.get("technical_rationale","").strip()
    disclaimer         = data.get("disclaimer","").strip() or "Не является инвестиционной рекомендацией. DYOR."

    # текстовый пост (как в канале)
    lines = []
    lines.append(f"🕗 Время (МСК): {time_msk}  💰 Текущая цена: {price}")
    lines.append(f"📊 Актив: {symbol}")
    lines.append("")
    lines.append("1️⃣ Почему выбран актив")
    lines.append(why_asset or "—")
    lines.append("")
    lines.append("2️⃣ Сетап")
    lines.append(f"**Направление:** {direction or '—'}")
    lines.append(f"**Диапазон входа:** {er_min}–{er_max}")
    lines.append(f"**SL:** {sl}")
    lines.append(f"**TP1:** {tp1}")
    lines.append(f"**TP2:** {tp2}")
    lines.append(f"**R:R:** {rr_str}")
    if take_profit_rules:
        lines.append(f"**Фиксация прибыли:** {take_profit_rules}")
    if break_even_rule:
        lines.append(f"**BE:** {break_even_rule}")
    lines.append("")
    lines.append("3️⃣ Картина по таймфреймам")
    tf_order = ["m5","m15","h1","h4","d1"]
    tf_parts = []
    for k in tf_order:
        if k in mtf:
            tf_parts.append(f"{k.replace('m','').replace('h','').upper() if k!='d1' else '1D'}: {mtf[k]}")
    # если нет порядковых — выведем всё что есть
    if not tf_parts:
        tf_parts = [f"{k}: {v}" for k,v in mtf.items()]
    lines.append("; ".join(tf_parts) if tf_parts else "—")
    lines.append("")
    lines.append("4️⃣ Новостной фон")
    if news_ctx:
        for it in news_ctx[:4]:
            lines.append(str(it))
    else:
        lines.append("Новостных триггеров не выявлено.")
    lines.append("")
    lines.append("5️⃣ Контекст рынка")
    lines.append(market_ctx or "—")
    lines.append("")
    lines.append("6️⃣ Валидность сигнала")
    lines.append(f"{validity} минут; {cancel_cond or '—'}")
    lines.append("")
    lines.append("⚙️ Обоснование")
    lines.append(rationale or "—")
    lines.append("")
    lines.append("⚠️ Дисклеймер")
    lines.append(disclaimer)

    text_out = trim_all_numbers("\n".join(lines))

    # HTML-пост (тот же контент, но с <b> вместо **)
    html = (text_out
            .replace("**","")  # уже без md в html-версии
           )
    # упрощённый html-конверт: \n -> <br>
    html = html.replace("&", "&amp;").replace("<","&lt;").replace(">","&gt;")
    html = html.replace("\n", "<br>\n")

    # сохранить HTML
    ts = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d_%H%M%S")
    out_name = f"signal_{ts}.html"
    pathlib.Path(out_name).write_text(html, encoding="utf-8")

    # вывести текст в stdout (для канала/лога) и сообщить про html
    print(text_out)
    print(f"\n[OK] HTML сохранён: {out_name}")

if __name__ == "__main__":
    main()
