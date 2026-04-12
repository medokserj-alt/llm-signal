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


def format_price(symbol: str | None, x, *, exchange=None) -> str:
    """
    Render prices with symbol-aware precision, mirroring get_signal_json._price_precision():
      - if value_hint < 10 => precision >= 4
      - else use exchange precision when available
    """
    try:
        v = float(x)
    except Exception:
        return str(x)
    if not math.isfinite(v):
        return str(x)
    try:
        prec = get_signal_json._price_precision(  # type: ignore[attr-defined]
            symbol if isinstance(symbol, str) else None,
            exchange=exchange,
            value_hint=v,
        )
    except Exception:
        prec = 4 if abs(v) < 10 else 2
    return f"{v:.{int(prec)}f}"


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

def _sanitize_trend_narrative(
    text: str,
    *,
    price_vs_ema20: str | None,
    ema_fan_state: str | None,
) -> str:
    """
    UX-only guard: if computed evidence is bearish/under-EMA, don't let narrative claim a stable uptrend / above-EMA.
    Keep short and do not introduce new indicators beyond the provided flags.
    """

    raw = _one_line(text or "")
    if not raw or raw == "—":
        return raw or "—"

    pv = (price_vs_ema20 or "").strip().lower() if isinstance(price_vs_ema20, str) else ""
    fan = (ema_fan_state or "").strip().lower() if isinstance(ema_fan_state, str) else ""
    bearish = pv == "below" or fan == "bear"
    if not bearish:
        return raw

    bullish_claim = re.search(
        r"(?i)\b("
        r"ап[\s-]*тренд|up[\s-]*trend|"
        r"быч\w*|bull\w*|"
        r"(выше|над)\s+ema\s*20|above\s+ema\s*20"
        r")\b",
        raw,
    )
    if not bullish_claim:
        return raw

    parts: list[str] = []
    if fan == "bear":
        parts.append("нисходящая фаза/коррекция")
    if pv == "below":
        parts.append("ниже EMA20")
    if not parts:
        parts.append("коррекционная фаза")
    return ", ".join(parts)


def _format_news_item(item) -> str:
    if isinstance(item, str):
        return _one_line(item)
    if isinstance(item, dict):
        required_keys = ("title", "impact", "time_msk", "url", "summary")
        if all(isinstance(item.get(key), str) and _one_line(item.get(key, "")) for key in required_keys):
            return (
                f"[{_one_line(item['time_msk'])}] "
                f"[impact:{_one_line(item['impact'])}] "
                f"{_one_line(item['title'])} — {_one_line(item['url'])} — {_one_line(item['summary'])}"
            )
        return _one_line(str(item))
    return _one_line(str(item))


def _render_news_context(news_context, *, limit: int = 3) -> list[str]:
    if news_context is None:
        return ["Новостной фон: —"]
    if isinstance(news_context, str):
        text = _one_line(news_context)
        return [text] if text else ["Новостной фон: —"]
    if not isinstance(news_context, list):
        text = _one_line(str(news_context))
        return [text] if text else ["Новостной фон: —"]

    lines: list[str] = []
    for item in news_context:
        if len(lines) >= limit:
            break
        text = _format_news_item(item)
        if text:
            lines.append(text)
    return lines or ["Новостной фон: —"]


def _infer_mtf_fallback(data: dict) -> str:
    raw_side = data.get("side")
    raw_direction = raw_side if (isinstance(raw_side, str) and raw_side.strip()) else data.get("direction")
    direction = _one_line(str(raw_direction)).lower() if raw_direction is not None else ""

    ema_signals = [
        data.get("price_vs_ema20_m15"),
        data.get("price_vs_ema20_h1"),
        data.get("ema20_m15"),
        data.get("ema20_h1"),
        data.get("ema_fan_m15_state"),
        data.get("ema_fan_h1_state"),
    ]
    has_ema_context = any(value not in (None, "", "—") for value in ema_signals)
    if direction in ("long", "short") or has_ema_context:
        if direction == "short":
            return "Таймфреймы: 5m–1h: структура соответствует направлению сделки, откаты к EMA используются как точки входа для short."
        return "Таймфреймы: 5m–1h: структура соответствует направлению сделки, откаты к EMA используются как точки входа."
    return "Таймфреймы: структура не определена"


def _render_mtf_block(data: dict, mtf: dict, mtf_fallback: str) -> list[str]:
    lines: list[str] = []
    ema_line = _ema_status_line(data)
    if ema_line:
        lines.append(ema_line)

    if mtf_fallback:
        lines.append(f"Таймфреймы: {_one_line(mtf_fallback)}")
        return lines

    tf_order = ("m5", "m15", "h1", "h4", "d1")
    has_meaningful_views = any(_one_line(str(mtf.get(tf, ""))) not in ("", "—") for tf in tf_order)
    if not has_meaningful_views:
        lines.append(_infer_mtf_fallback(data))
        return lines

    m15_view = _sanitize_trend_narrative(
        str(mtf.get("m15", "—")),
        price_vs_ema20=data.get("price_vs_ema20_m15"),
        ema_fan_state=data.get("ema_fan_m15_state"),
    )
    h1_view = _sanitize_trend_narrative(
        str(mtf.get("h1", "—")),
        price_vs_ema20=data.get("price_vs_ema20_h1"),
        ema_fan_state=data.get("ema_fan_h1_state"),
    )
    lines.append(
        "5m: "
        f"{_one_line(str(mtf.get('m5','—')))}; 15m: {_one_line(m15_view)}; "
        f"1h: {_one_line(h1_view)}; 4h: {_one_line(str(mtf.get('h4','—')))}; "
        f"1D: {_one_line(str(mtf.get('d1','—')))}"
    )
    return lines


def _iter_event_risk_lines(d: dict) -> list[str]:
    event_risk = d.get("event_risk")
    if not isinstance(event_risk, dict):
        return []
    raw = event_risk.get("display_lines")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = _one_line(item)
        if text:
            out.append(text)
    return out


def _iter_flow_overlay_lines(d: dict) -> list[str]:
    flow_overlay = d.get("flow_overlay")
    if not isinstance(flow_overlay, dict):
        return []
    raw = flow_overlay.get("display_lines")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = _one_line(item)
        if text:
            out.append(text)
    return out


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

    price = format_price(symbol if isinstance(symbol, str) else None, data.get("price", ""))

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
    raw_mtf = data.get("multi_tf_view")
    if isinstance(raw_mtf, dict):
        mtf = raw_mtf
        mtf_fallback = ""
    elif isinstance(raw_mtf, str):
        mtf = {}
        mtf_fallback = raw_mtf.strip()
    else:
        mtf = {}
        mtf_fallback = ""
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
            rr_val = _valid_price(data.get("rr"))
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
            try:
                ctx = data.get("day_mid_context")
                if isinstance(ctx, dict):
                    note = (ctx.get("override_note") or "").strip()
                    if note:
                        lines.append(note)
            except Exception:
                pass
            for event_line in _iter_event_risk_lines(data)[:2]:
                lines.append(event_line)
            for flow_line in _iter_flow_overlay_lines(data)[:2]:
                lines.append(flow_line)
            wl = " ".join(w.lower() for w in _iter_warnings(data))
            if mode == "aggressive" and "phase_flip_wait_confirm" in wl:
                lines.append("⚠️ Phase flip по M15: вход только после подтверждения (wait_confirm).")
            if mode == "aggressive" and "aggressive_countertrend_no_evidence_wait_confirm" in wl:
                lines.append("⚠️ Контртренд против сильного H1 — вход только после подтверждения (wait_confirm).")
            if mode == "aggressive" and bool(data.get("is_us_open_block")):
                lines.append(
                    "⚠️⚠️ USA OPEN (17:00–19:30 МСК): HIGH VOLATILITY / FAKE MOVES — WAIT CONFIRM ⚠️⚠️"
                )
            for w in _iter_warnings(data):
                if "Низкая ликвидность (ночное окно)" in w:
                    lines.append(w)
                    break

            # wait_confirm UX: keep it user-simple (no checklists / ranges in text).
            em_raw = (data.get("entry_mode") or "").strip().lower()
            is_wait_confirm = em_raw in ("wait_confirm", "confirm", "wait-confirm", "wc")
            if is_wait_confirm:
                lines.append("⏳ Вход: wait_confirm")
                lines.append("ℹ️ Подтверждение/снятие сценария — через AIA (если подключён).")

            raw_side = data.get("side")
            raw_dir = raw_side if (isinstance(raw_side, str) and raw_side.strip()) else data.get("direction")
            side_key = (raw_dir or "").strip().lower() if isinstance(raw_dir, str) else ""

            entry_line = f"Вход: {format_price(symbol if isinstance(symbol, str) else None, entry_val)}"
            px = _as_float(data.get("price"))
            if px is not None and px > 0 and entry_val is not None:
                delta_pct = abs(entry_val - px) / px * 100.0

                if side_key in ("long", "short"):
                    correct_side = (side_key == "long" and entry_val <= px) or (side_key == "short" and entry_val >= px)
                    strange_side = (side_key == "long" and entry_val > px) or (side_key == "short" and entry_val < px)
                    near_and_correct_side = delta_pct <= THRESHOLD_NEAR_PCT and correct_side

                    if mode == "aggressive" and near_and_correct_side and not is_wait_confirm:
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
            lines.append(f"SL: {format_price(symbol if isinstance(symbol, str) else None, sl_val)}")
            if mode == "aggressive":
                lines.append(f"TP1: {format_price(symbol if isinstance(symbol, str) else None, tp1_val)}")
                lines.append(f"TP2: {format_price(symbol if isinstance(symbol, str) else None, tp2_val)}")
                if tp3_val is not None:
                    lines.append(f"TP3: {format_price(symbol if isinstance(symbol, str) else None, tp3_val)}")
            elif mode == "neutral":
                lines.append(f"TP1: {format_price(symbol if isinstance(symbol, str) else None, tp1_val)}")
                lines.append(f"TP2: {format_price(symbol if isinstance(symbol, str) else None, tp2_val)}")
            else:
                lines.append(f"TP1: {format_price(symbol if isinstance(symbol, str) else None, tp1_val)}")
                if tp2_or_trail == "trail":
                    lines.append("TP2_or_trail: trail")
                else:
                    lines.append(
                        f"TP2_or_trail: {format_price(symbol if isinstance(symbol, str) else None, tp2_or_trail)}"
                    )
                lines.append("Горизонт: 1–3 дня (conservative)")
            lines.append(f"RR: 1:{fmt(rr_val)}")
            lines.append(f"План выхода: {exit_plan}")
            lines.append("")

        # 3) Таймфреймы
        if mode_valid:
            lines.append("3️⃣ Таймфреймы")
            lines.extend(_render_mtf_block(data, mtf, mtf_fallback))
            lines.append("")

        if mode_valid:
            # 4) Новостной фон (1–3 строки, как в news_snapshot.py)
            lines.append("4️⃣ Новостной фон")
            lines.extend(_render_news_context(news_ctx, limit=3))
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
