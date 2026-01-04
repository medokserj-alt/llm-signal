#!/usr/bin/env python3
import sys
import json
import re
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo
import math

import get_signal_json

from no_trade_explain import (
    ensure_decision_path,
    format_no_trade_message,
    infer_mode_reject_reason_key,
    reason_to_short_text,
)

VALID_MODES = {"aggressive", "neutral", "conservative"}

# Entry considered "near current" if mid(range) is within this % delta from current price.
# Keep conservative to avoid false "enter now" messaging.
THRESHOLD_NEAR_PCT = 0.15


def normalize_mode(mode_val) -> str:
    try:
        m = (mode_val or "").strip().lower()
    except Exception:
        m = ""
    return m if m in VALID_MODES else "neutral"


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
    mode = normalize_mode(data.get("mode"))
    if mode != "aggressive":
        return data

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

def _ema_status_line(data: dict) -> str | None:
    def norm_flag(v) -> str | None:
        if not isinstance(v, str):
            return None
        v = v.strip().lower()
        return v if v in ("above", "below", "equal") else None

    def compute_flag(price, ema) -> str | None:
        try:
            p = float(price)
            e = float(ema)
        except Exception:
            return None
        tol = max(abs(p) * 1e-6, 1e-12)
        if abs(p - e) <= tol:
            return "equal"
        return "above" if p > e else "below"

    def label(flag: str | None) -> str:
        return {"above": "ВЫШЕ EMA20", "below": "НИЖЕ EMA20", "equal": "У EMA20"}.get(flag or "", "—")

    m15 = norm_flag(data.get("price_vs_ema20_m15")) or compute_flag(data.get("price"), data.get("ema20_m15"))
    h1 = norm_flag(data.get("price_vs_ema20_h1")) or compute_flag(data.get("price"), data.get("ema20_h1"))
    if not m15 and not h1:
        return None
    return f"EMA статус: M15: {label(m15)} | H1: {label(h1)}"


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERR: empty stdin", file=sys.stderr)
        sys.exit(1)
    data = _drop_time_window_mentions(json.loads(raw))

    # базовые поля
    time_msk = data.get("time_msk", now_msk())
    symbol = data.get("symbol", "?")
    try:
        price_prec = get_signal_json._price_precision(  # type: ignore[attr-defined]
            symbol if isinstance(symbol, str) else None,
            value_hint=float(data.get("price")) if data.get("price") is not None else None,
        )
    except Exception:
        price_prec = 2

    def fmt_price(x) -> str:
        try:
            v = float(x)
        except Exception:
            return str(x)
        if not math.isfinite(v):
            return str(x)
        return f"{v:.{int(price_prec)}f}"

    price = fmt_price(data.get("price", ""))

    mode = normalize_mode(data.get("mode"))
    requested_mode_raw = data.get("requested_mode") if isinstance(data, dict) else None
    requested_mode = (
        normalize_mode(requested_mode_raw) if requested_mode_raw is not None else None
    )

    def _direction_badge(d: dict) -> str:
        raw_side = d.get("side")
        raw = raw_side
        if not (isinstance(raw_side, str) and raw_side.strip()):
            raw = d.get("direction")
        if isinstance(raw, str):
            key = raw.strip().lower()
        else:
            key = (str(raw).strip().lower() if raw is not None else "")
        return {"long": "🟩 LONG", "short": "🟥 SHORT"}.get(key, "")

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
        ensure_decision_path(data)
        text_out = format_no_trade_message(data)
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
        lines.append(_one_line(why_asset) or "—")
        lines.append("")

        # 2) Сетап (ТОЛЬКО текущий режим)
        MODE_LABELS = {
            "aggressive": "🟥 Агрессивный",
            "neutral": "🟨 Нейтральный",
            "conservative": "🟩 Консервативный",
        }

        def _iter_warnings(d: dict) -> list[str]:
            w = d.get("warnings")
            if not isinstance(w, list):
                return []
            out: list[str] = []
            for it in w:
                if isinstance(it, str) and it.strip():
                    out.append(it.strip())
            return out

        def _is_between_ema_m15_h1(d: dict) -> bool:
            def norm_flag(v) -> str | None:
                if not isinstance(v, str):
                    return None
                v = v.strip().lower()
                return v if v in ("above", "below", "equal") else None

            m15 = norm_flag(d.get("price_vs_ema20_m15"))
            h1 = norm_flag(d.get("price_vs_ema20_h1"))
            if m15 and h1:
                if m15 == "equal" or h1 == "equal":
                    return True
                return m15 != h1

            ema_guard = d.get("ema_guard")
            if isinstance(ema_guard, dict):
                st = str(ema_guard.get("state") or "").strip().lower()
                return st == "between"
            return False

        def _downgrade_reason_text(d: dict, src_mode: str) -> str:
            keys: list[str] = []

            path = d.get("decision_path") if isinstance(d.get("decision_path"), list) else None
            if path is None:
                ensure_decision_path(d)
                path = d.get("decision_path") if isinstance(d.get("decision_path"), list) else []

            for step in path or []:
                if not isinstance(step, dict):
                    continue
                if step.get("result") != "rejected":
                    continue
                if normalize_mode(step.get("mode")) != src_mode:
                    continue
                rk = str(step.get("reason") or "").strip()
                if rk:
                    keys.append(rk)

            # Fallback to direct inference from existing fields.
            if not keys:
                keys.append(infer_mode_reject_reason_key(d, src_mode))

            # Add up to one extra "headline" reason if present.
            wl = " ".join(w.lower() for w in _iter_warnings(d))
            if "time_window" in wl:
                keys.append("time_window")
            elif "risk_off" in wl:
                keys.append("risk_off")
            elif "impulse_no_exhale" in wl:
                keys.append("impulse_no_exhale")
            elif "phase_between" in wl:
                keys.append("phase_between")
            elif _is_between_ema_m15_h1(d):
                keys.append("ema_guard_between")

            seen: set[str] = set()
            out: list[str] = []
            for k in keys:
                kk = (k or "").strip()
                if not kk:
                    continue
                if kk in seen:
                    continue
                seen.add(kk)
                txt = reason_to_short_text(kk, src_mode).strip()
                if txt:
                    out.append(txt)
                if len(out) >= 2:
                    break
            return "; ".join(out[:2])

        def _as_float(x):
            try:
                v = float(x)
            except Exception:
                return None
            if not math.isfinite(v):
                return None
            return v

        def _valid_price(x) -> float | None:
            v = _as_float(x)
            if v is None or not v:
                return None
            return v

        def _mode_enabled_for_active_mode() -> bool:
            try:
                bucket = entries.get(mode) if isinstance(entries, dict) else {}
                bucket = bucket if isinstance(bucket, dict) else {}
                enabled = bucket.get("enabled", True)
                return enabled is not False
            except Exception:
                return True

        missing: list[str] = []
        if not _mode_enabled_for_active_mode():
            missing.append("режим отключён")

        entry_val = _valid_price(data.get(f"entry_price_{mode}"))
        if entry_val is None:
            missing.append("цена входа")

        sl_by_mode = data.get("sl_by_mode") if isinstance(data.get("sl_by_mode"), dict) else {}
        sl_val = _valid_price(sl_by_mode.get(mode))
        if sl_val is None:
            missing.append("SL")

        tp_by_mode = data.get("tp_by_mode") if isinstance(data.get("tp_by_mode"), dict) else {}
        tp_bucket = tp_by_mode.get(mode) if isinstance(tp_by_mode.get(mode), dict) else {}

        def _tp_num(*keys: str) -> float | None:
            for k in keys:
                if k in tp_bucket:
                    v = _valid_price(tp_bucket.get(k))
                    if v is not None:
                        return v
            return None

        tp1_val = _tp_num("tvh1", "tp1")
        if tp1_val is None:
            missing.append("TP1")

        tp2_val = None
        tp3_val = None
        tp2_or_trail = None
        if mode == "aggressive":
            tp2_val = _tp_num("tvh2", "tp2")
            if tp2_val is None:
                missing.append("TP2")
            tp3_val = _tp_num("tvh3", "tp3")  # optional (can be None)
        elif mode == "neutral":
            tp2_val = _tp_num("tvh2", "tp2")
            if tp2_val is None:
                missing.append("TP2")
        else:  # conservative
            raw = tp_bucket.get("tvh2_or_trail")
            if isinstance(raw, str) and raw.strip().lower() == "trail":
                tp2_or_trail = "trail"
            else:
                v = _valid_price(raw)
                if v is None:
                    v = _tp_num("tp2")
                tp2_or_trail = v
            if tp2_or_trail is None:
                missing.append("TP2_or_trail")

        rr_by_mode = data.get("rr_by_mode") if isinstance(data.get("rr_by_mode"), dict) else {}
        rr_val = _valid_price(rr_by_mode.get(mode))
        if rr_val is None:
            missing.append("RR")

        exit_plan_by_mode = (
            data.get("exit_plan_by_mode") if isinstance(data.get("exit_plan_by_mode"), dict) else {}
        )
        exit_plan = exit_plan_by_mode.get(mode)
        exit_plan = _one_line(exit_plan) if isinstance(exit_plan, str) else ""
        if not exit_plan:
            missing.append("план выхода")

        mode_valid = not missing
        if not mode_valid:
            reason = f"Невалидные данные для режима {mode}: " + ", ".join(missing) + "."
            reason = _truncate(reason, 220).rstrip(".").rstrip()
            text_out = "\n".join(
                [
                    "📌 Сигнал не выдан",
                    f"Причина: {reason}.",
                    "Я продолжу мониторить рынок и дам обновление при появлении надёжного сетапа.",
                ]
            )
        else:
            lines.append("2️⃣ Сетап")
            lines.append(f"Режим: {MODE_LABELS.get(mode, mode)}")
            if requested_mode and requested_mode != mode:
                lines.append(f"🔁 Downgrade: {requested_mode} → {mode}")
                reason = _downgrade_reason_text(data, requested_mode)
                if reason:
                    lines.append(f"Причина: {reason}")
            lines.append(f"Направление: {_direction_badge(data) or '—'}")

            raw_side = data.get("side")
            raw_dir = raw_side if (isinstance(raw_side, str) and raw_side.strip()) else data.get("direction")
            side_key = (raw_dir or "").strip().lower() if isinstance(raw_dir, str) else ""

            entry_line = f"Вход: {fmt_price(entry_val)}"
            px = _as_float(data.get("price"))
            if px is not None and px > 0 and entry_val is not None:
                delta_pct = abs(entry_val - px) / px * 100.0

                if side_key in ("long", "short"):
                    correct_side = (side_key == "long" and entry_val <= px) or (side_key == "short" and entry_val >= px)
                    strange_side = (side_key == "long" and entry_val > px) or (side_key == "short" and entry_val < px)
                    near_and_correct_side = delta_pct <= THRESHOLD_NEAR_PCT and correct_side

                    if mode == "aggressive" and near_and_correct_side:
                        lines.append("**✅ Логичен вход от текущей / вблизи текущей (агрессивно).**")
                    elif strange_side:
                        if side_key == "long":
                            lines.append(
                                "⚠️ Вход расположен *выше текущей цены* (для LONG): это не вход по рынку, а активация при достижении уровня."
                            )
                        else:
                            lines.append(
                                "⚠️ Вход расположен *ниже текущей цены* (для SHORT): это не вход по рынку, а активация при достижении уровня."
                            )

            lines.append(entry_line)
            aggressive_option = data.get("aggressive_option")
            if mode == "neutral" and isinstance(aggressive_option, dict):
                a_entry = _valid_price(aggressive_option.get("entry_price"))
                if a_entry is None:
                    a_entry = _valid_price(data.get("entry_price_aggressive"))
                if a_entry is not None:
                    lines.append(
                        f"⚡ Возможен агрессивный вход: {fmt_price(a_entry)} (повышенный риск)."
                    )
                    if data.get("neutral_autosplit") is True:
                        lines.append(
                            "ℹ️ Neutral-вход отодвинут от текущей цены; ранний вход вынесен в aggressive option."
                        )
                    side_ok = False
                    try:
                        buffer_ticks = int(get_signal_json.NEUTRAL_BUFFER_TICKS)  # type: ignore[attr-defined]
                    except Exception:
                        buffer_ticks = 10
                    tick = 10 ** (-int(price_prec))
                    buf = float(buffer_ticks) * float(tick)
                    if entry_val is not None:
                        if side_key == "long":
                            side_ok = float(entry_val) <= float(a_entry) - buf
                        elif side_key == "short":
                            side_ok = float(entry_val) >= float(a_entry) + buf
                    if side_ok and data.get("neutral_autosplit") is not True:
                        lines.append("ℹ️ Neutral-вход выставлен с запасом относительно aggressive.")
            lines.append(f"SL: {fmt_price(sl_val)}")
            if mode == "aggressive":
                lines.append(f"TP1: {fmt_price(tp1_val)}")
                lines.append(f"TP2: {fmt_price(tp2_val)}")
                if tp3_val is not None:
                    lines.append(f"TP3: {fmt_price(tp3_val)}")
            elif mode == "neutral":
                lines.append(f"TP1: {fmt_price(tp1_val)}")
                lines.append(f"TP2: {fmt_price(tp2_val)}")
            else:
                lines.append(f"TP1: {fmt_price(tp1_val)}")
                if tp2_or_trail == "trail":
                    lines.append("TP2_or_trail: trail")
                else:
                    lines.append(f"TP2_or_trail: {fmt_price(tp2_or_trail)}")
            lines.append(f"RR: 1:{fmt(rr_val)}")
            lines.append(f"План выхода: {exit_plan}")
            lines.append("")

        # 3) Таймфреймы
        if mode_valid:
            lines.append("3️⃣ Таймфреймы")
            ema_line = _ema_status_line(data)
            if ema_line:
                lines.append(ema_line)
            lines.append(
                "5m: "
                f"{_one_line(str(mtf.get('m5','—')))}; 15m: {_one_line(str(mtf.get('m15','—')))}; "
                f"1h: {_one_line(str(mtf.get('h1','—')))}; 4h: {_one_line(str(mtf.get('h4','—')))}; "
                f"1D: {_one_line(str(mtf.get('d1','—')))}"
            )
            lines.append("")

        if mode_valid:
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

            # Дисклеймер (одна строка)
            lines.append("⚠️ Дисклеймер")
            lines.append(_one_line(disclaimer) or "—")
            text_out = "\n".join(lines)

    # Keep price precision in rendered text (no global trimming).

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
