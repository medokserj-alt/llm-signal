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

def _now_msk_news_prefix() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M")

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

def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _truncate(s: str, limit: int) -> str:
    s = _one_line(s)
    if not s or len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut.rstrip(".,;:—- ") + "…"


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

    if no_trade:
        reason = _one_line(no_trade_hint)
        if not reason:
            for r in no_trade_reasons:
                if isinstance(r, str) and _one_line(r):
                    reason = _one_line(r)
                    break
        reason = _truncate(reason or "Недостаточно надёжного сетапа", 220)
        reason = reason.rstrip(".").rstrip()

        text_out = "\n".join(
            [
                "📌 Сигнал не выдан",
                f"Причина: {reason}.",
                "Я продолжу мониторить рынок и дам обновление при появлении надёжного сетапа.",
            ]
        )
    else:
        # --- формируем текст (короткий формат Димы) ---
        lines: list[str] = []

        # Шапка
        lines.append("📣 Сигнал")
        lines.append(f"🕗 Время (МСК): {time_msk}  💰 Текущая цена: {price}")
        lines.append(f"📊 Актив: {symbol}")
        lines.append("")

        # 1) Почему выбран актив
        lines.append("1️⃣ Почему выбран актив")
        lines.append(_truncate(why_asset or "—", 240) or "—")
        lines.append("")

        # 2) Сетап
        agg = entries.get("aggressive") or {}
        neu = entries.get("neutral") or {}
        con = entries.get("conservative") or {}

        def _mode_enabled(mode_dict: dict) -> bool:
            try:
                return bool(mode_dict.get("enabled", True))
            except Exception:
                return True

        def _entry_price(key: str, enabled: bool) -> str:
            if not enabled:
                return "—"
            v = data.get(key)
            return fmt(v) if v is not None else "—"

        entry_price_aggressive = _entry_price("entry_price_aggressive", _mode_enabled(agg))
        entry_price_neutral = _entry_price("entry_price_neutral", _mode_enabled(neu))
        entry_price_conservative = _entry_price("entry_price_conservative", _mode_enabled(con))

        sl_by_mode = data.get("sl_by_mode") if isinstance(data.get("sl_by_mode"), dict) else None
        tp_by_mode = data.get("tp_by_mode") if isinstance(data.get("tp_by_mode"), dict) else None
        rr_by_mode = data.get("rr_by_mode") if isinstance(data.get("rr_by_mode"), dict) else None
        if sl_by_mode is None:
            sl_by_mode = {"aggressive": sl, "neutral": sl, "conservative": sl}
        if tp_by_mode is None:
            tp_by_mode = {
                "aggressive": {"tvh1": tp1, "tvh2": tp2, "tvh3": "—"},
                "neutral": {"tvh1": tp1, "tvh2": tp2},
                "conservative": {"tvh1": tp1, "tvh2_or_trail": tp2},
            }
        if rr_by_mode is None:
            rr_by_mode = {"aggressive": rr, "neutral": rr, "conservative": rr}

        def _rr_value(x) -> str:
            if x is None or x == "":
                return "—"
            if isinstance(x, (int, float)):
                return fmt(x)
            s = _one_line(str(x))
            s = s.replace("RR", "").strip()
            if s.startswith("1:"):
                s = s[2:].strip()
            return s or "—"

        def _tvh(mode: str, key: str, fallback: str = "—") -> str:
            bucket = tp_by_mode.get(mode) or {}
            if key in bucket and bucket.get(key) is not None and bucket.get(key) != "":
                return fmt(bucket.get(key))
            # поддержка legacy tp1/tp2
            if key == "tvh1" and bucket.get("tp1") is not None:
                return fmt(bucket.get("tp1"))
            if key in ("tvh2", "tvh2_or_trail") and bucket.get("tp2") is not None:
                return fmt(bucket.get("tp2"))
            return fmt(fallback) if fallback not in ("—", "", None) else "—"

        def _sl(mode: str) -> str:
            v = sl_by_mode.get(mode, sl)
            return fmt(v) if v not in ("", None) else "—"

        lines.append("2️⃣ Сетап")
        lines.append(f"Направление: {direction}")
        lines.append(
            "Цена входа: "
            f"Agg {entry_price_aggressive} | Neutral {entry_price_neutral} | Cons {entry_price_conservative}"
        )
        lines.append(
            "SL: "
            f"Agg {_sl('aggressive')} | Neutral {_sl('neutral')} | Cons {_sl('conservative')}"
        )
        lines.append("ТВХ:")
        lines.append("")
        agg_bucket = tp_by_mode.get("aggressive") or {}
        show_tvh3 = ("tvh3" in agg_bucket) and (agg_bucket.get("tvh3") is not None) and (agg_bucket.get("tvh3") != "")
        lines.append(
            "Agg: "
            f"ТВХ1 {_tvh('aggressive','tvh1')} | ТВХ2 {_tvh('aggressive','tvh2')}"
            + (f" | ТВХ3 {_tvh('aggressive','tvh3')}" if show_tvh3 else "")
            + f" | RR 1:{_rr_value(rr_by_mode.get('aggressive'))}"
        )
        lines.append("")
        lines.append(
            "Neutral: "
            f"ТВХ1 {_tvh('neutral','tvh1')} | ТВХ2 {_tvh('neutral','tvh2')} | "
            f"RR 1:{_rr_value(rr_by_mode.get('neutral'))}"
        )
        lines.append("")
        lines.append(
            "Cons: "
            f"ТВХ1 {_tvh('conservative','tvh1')} | ТВХ2/Trail {_tvh('conservative','tvh2_or_trail')} | "
            f"RR 1:{_rr_value(rr_by_mode.get('conservative'))}"
        )
        lines.append("")

        exit_plan_by_mode = (
            data.get("exit_plan_by_mode") if isinstance(data.get("exit_plan_by_mode"), dict) else None
        )
        if exit_plan_by_mode is None:
            plan = " ".join(x for x in [take_profit_rules, break_even_rule] if x).strip()
            exit_plan_by_mode = {"aggressive": plan, "neutral": plan, "conservative": plan}

        lines.append("План выхода:")
        lines.append(f"Agg: {_one_line(exit_plan_by_mode.get('aggressive') or '') or '—'}")
        lines.append(f"Neutral: {_one_line(exit_plan_by_mode.get('neutral') or '') or '—'}")
        lines.append(f"Cons: {_one_line(exit_plan_by_mode.get('conservative') or '') or '—'}")
        lines.append("")

        # 3) Таймфреймы
        lines.append("3️⃣ Таймфреймы")
        lines.append(
            "5m: "
            f"{_one_line(str(mtf.get('m5','—')))}; 15m: {_one_line(str(mtf.get('m15','—')))}; "
            f"1h: {_one_line(str(mtf.get('h1','—')))}; 4h: {_one_line(str(mtf.get('h4','—')))}; "
            f"1D: {_one_line(str(mtf.get('d1','—')))}"
        )
        lines.append("")

        # 4) Новостной фон (1–3 строки, как в news_snapshot.py)
        lines.append("4️⃣ Новостной фон")
        news_lines: list[str] = []
        for it in (news_ctx or []):
            if len(news_lines) >= 3:
                break
            s = _one_line(str(it))
            if s:
                news_lines.append(s)
        if not news_lines:
            news_lines = [
                f"- [{_now_msk_news_prefix()} МСК] [impact:neutral] Новостных триггеров не выявлено."
            ]
        lines.extend(news_lines)
        lines.append("")

        # 5) Контекст рынка (одна строка)
        lines.append("5️⃣ Контекст рынка")
        lines.append(_truncate(market_ctx or "—", 220) or "—")
        lines.append("")

        # Обоснование (коротко)
        lines.append("⚙️ Обоснование")
        lines.append(_truncate(rationale or "—", 650) or "—")
        lines.append("")

        # Дисклеймер (одна строка)
        lines.append("⚠️ Дисклеймер")
        lines.append(_one_line(disclaimer) or "—")

        text_out = trim_all_numbers("\n".join(lines))

    text_out = trim_all_numbers(text_out)

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


if __name__ == "__main__":
    main()
