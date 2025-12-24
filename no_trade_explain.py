from __future__ import annotations

import re
from typing import Any


VALID_MODES = {"aggressive", "neutral", "conservative"}


def normalize_mode(mode_val: Any) -> str:
    try:
        m = (mode_val or "").strip().lower()
    except Exception:
        m = ""
    return m if m in VALID_MODES else "neutral"


MODE_LABELS_RU = {
    "aggressive": "Агрессивный",
    "neutral": "Нейтральный",
    "conservative": "Консервативный",
}

MODE_LABELS_RU_GEN = {
    "aggressive": "агрессивного",
    "neutral": "нейтрального",
    "conservative": "консервативного",
}

MODE_LABELS_RU_PREP = {
    "aggressive": "агрессивном",
    "neutral": "нейтральном",
    "conservative": "консервативном",
}


def _iter_str_list(x: Any) -> list[str]:
    if not isinstance(x, list):
        return []
    out: list[str] = []
    for it in x:
        if isinstance(it, str):
            s = it.strip()
            if s:
                out.append(s)
    return out


def _get_warnings(d: dict) -> list[str]:
    return _iter_str_list(d.get("warnings"))


def _parse_mode_fallback(warnings: list[str]) -> tuple[str | None, str | None]:
    # Example: "mode_fallback: aggressive->neutral"
    for w in reversed(warnings):
        m = re.search(r"\bmode_fallback:\s*([a-z_]+)->([a-z_]+)\b", w.strip().lower())
        if not m:
            continue
        src = normalize_mode(m.group(1))
        dst = normalize_mode(m.group(2))
        if src and dst:
            return src, dst
    return None, None


def _disabled_by_tags(d: dict, mode: str) -> list[str]:
    entries = d.get("entries") if isinstance(d.get("entries"), dict) else {}
    bucket = entries.get(mode) if isinstance(entries.get(mode), dict) else {}
    tags = bucket.get("disabled_by")
    if isinstance(tags, str) and tags.strip():
        return [tags.strip()]
    if isinstance(tags, list):
        return [str(x).strip() for x in tags if str(x).strip()]
    return []


def _infer_primary_no_trade_reason_key(d: dict) -> str:
    reasons = _iter_str_list(d.get("no_trade_reasons"))
    hint = str(d.get("no_trade_hint") or "").strip()
    low_hint = hint.lower()

    if any(r == "waiting_confirmation" for r in reasons):
        return "waiting_confirmation"
    if any("time_window" in r for r in reasons) or ("окно времени" in low_hint) or ("ликвидност" in low_hint):
        return "time_window"
    if any(r == "invalid_mode_setup" for r in reasons):
        return "invalid_mode_setup"
    if any(r == "mode_disabled" for r in reasons):
        return "mode_disabled"
    if any(("rr" in r.lower()) or ("недостаточный rr" in r.lower()) for r in reasons) or ("rr" in low_hint):
        return "low_rr"

    if reasons:
        return reasons[0]
    if hint:
        return "no_trade_hint"
    return "no_setup"


def _infer_mode_reject_reason_key(d: dict, mode: str) -> str:
    tags = _disabled_by_tags(d, mode)
    if tags:
        return tags[0]

    warnings = _get_warnings(d)
    wl = " ".join(w.lower() for w in warnings)
    if "impulse_no_exhale" in wl:
        return "impulse_no_exhale"
    if "phase_between" in wl:
        return "phase_between"
    if "ema_between_m15_h1" in wl:
        return "ema_between_m15_h1"
    return "mode_disabled"


def build_decision_path(d: dict) -> list[dict]:
    """
    Builds a structured decision path from existing facts only (warnings/entries/no_trade fields).
    Does NOT change trading logic.
    """
    if not isinstance(d, dict):
        return []

    warnings = _get_warnings(d)
    src, dst = _parse_mode_fallback(warnings)
    final_mode = normalize_mode(d.get("mode"))
    requested_mode = src or final_mode
    if dst:
        final_mode = dst

    no_trade = bool(d.get("no_trade"))
    path: list[dict] = []

    if requested_mode != final_mode:
        path.append(
            {"mode": requested_mode, "result": "rejected", "reason": _infer_mode_reject_reason_key(d, requested_mode)}
        )
        if no_trade:
            path.append({"mode": final_mode, "result": "rejected", "reason": _infer_primary_no_trade_reason_key(d)})
            path.append({"mode": "final", "result": "no_trade"})
        else:
            path.append({"mode": final_mode, "result": "accepted"})
        return path

    # no fallback
    if no_trade:
        path.append({"mode": final_mode, "result": "rejected", "reason": _infer_primary_no_trade_reason_key(d)})
        path.append({"mode": "final", "result": "no_trade"})
    else:
        path.append({"mode": final_mode, "result": "accepted"})
    return path


def ensure_decision_path(d: dict) -> dict:
    if not isinstance(d, dict):
        return d
    d["decision_path"] = build_decision_path(d)
    return d


def classify_reason(reason_key: str) -> str:
    k = (reason_key or "").strip().lower()
    if not k:
        return "Structural"

    if k in {
        "waiting_confirmation",
        "impulse_no_exhale",
        "phase_between",
        "ema_between_m15_h1",
        "ema_guard_between",
        "ema_guard_below_both_long",
        "ema_guard_above_both_short",
        "entry_not_anchored_to_ema20_m15",
    }:
        return "Structural"

    if k in {"time_window", "risk_off", "time_window_low_liquidity"}:
        return "Risk"

    if k in {"mode_disabled", "invalid_mode_setup"} or k.startswith("ema_guard_"):
        return "Mode limitation"

    if k.startswith("disabled_by="):
        return "Mode limitation"

    # fallback: treat unknown as Structural (safer for traders than "technical")
    return "Structural"


def _reason_to_user_text(reason_key: str, mode: str | None = None) -> str:
    k = (reason_key or "").strip()
    low = k.lower()
    if low == "impulse_no_exhale":
        return "нет подтверждённой фазы выдоха после импульса на M15 (вход догоняет движение)."
    if low == "waiting_confirmation":
        return "ожидание подтверждения структуры/отката (фаза выдоха ещё не сформирована)."
    if low in {"phase_between", "ema_between_m15_h1", "ema_guard_between"}:
        return "цена/вход в зоне неопределённости между EMA20(M15) и EMA20(H1) — повышенный риск пилы."
    if low == "ema_guard_below_both_long":
        return "для LONG цена ниже EMA20 на M15 и H1 — агрессивный вход заблокирован фильтром."
    if low == "ema_guard_above_both_short":
        return "для SHORT цена выше EMA20 на M15 и H1 — агрессивный вход заблокирован фильтром."
    if low == "time_window":
        return "сейчас риск-окно по времени/ликвидности (без ухудшения качества входа сделку пропускаем)."
    if low == "low_rr":
        return "недостаточный R:R при текущем входе/SL/целях."
    if low == "invalid_mode_setup":
        return "некорректные/неполные уровни для выбранного режима (SL/TP/RR)."
    if low == "mode_disabled":
        m = normalize_mode(mode) if mode else ""
        suffix = f" ({m})" if m else ""
        return f"режим отключён фильтрами{suffix}."
    if low == "no_setup":
        return "недостаточно надёжного сетапа по текущей структуре."
    if low == "no_trade_hint":
        return "условия входа сейчас не соответствуют требованиям стратегии."
    # allow disabled_by tags to surface for transparency (trader-readable mapping above covers common ones)
    return k


def _what_must_change(reason_keys: list[str], mode: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(line: str) -> None:
        s = line.strip()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    for rk in reason_keys:
        low = (rk or "").strip().lower()
        if low in {"impulse_no_exhale", "waiting_confirmation"}:
            add("– Сформировать откат/«выдох» к EMA20(M15) и подтвердить структуру (без догоняющего входа).")
        if low in {"phase_between", "ema_between_m15_h1", "ema_guard_between"}:
            add("– Выйти из зоны между EMA20(M15) и EMA20(H1) и сформировать направленную структуру.")
        if low == "time_window":
            add("– Дождаться выхода из риск-окна по времени / ликвидности.")
        if low == "low_rr":
            add("– Улучшить R:R: более выгодный вход (глубже откат) или более понятная цель без роста риска.")
        if low == "ema_guard_below_both_long":
            add("– Для LONG: цена должна закрепиться выше EMA20(M15) и EMA20(H1).")
        if low == "ema_guard_above_both_short":
            add("– Для SHORT: цена должна закрепиться ниже EMA20(M15) и EMA20(H1).")

    if not out:
        add("– Дождаться формирования более чистой структуры/подтверждения по M15.")

    return out[:4]


def format_no_trade_message(d: dict) -> str:
    ensure_decision_path(d)
    path = d.get("decision_path") if isinstance(d.get("decision_path"), list) else []
    no_trade = bool(d.get("no_trade"))

    if not no_trade:
        return "📌 Сигнал не выдан\n\nПричина (No trade):\n\n• Нет данных: no_trade=false.\n\nСтатус: рынок в фазе ожидания, бот продолжает мониторинг."

    rejected_steps = [s for s in path if isinstance(s, dict) and s.get("result") == "rejected"]
    modes_in_path = [str(s.get("mode")) for s in rejected_steps if isinstance(s.get("mode"), str)]
    downgrade = len(modes_in_path) >= 2 and modes_in_path[0] != modes_in_path[1]

    lines: list[str] = []
    lines.append("📌 Сигнал не выдан")
    lines.append("")
    lines.append("Причина (No trade):")
    lines.append("")

    if rejected_steps:
        first = rejected_steps[0]
        m0 = normalize_mode(first.get("mode"))
        r0 = str(first.get("reason") or "").strip()
        cat0 = classify_reason(r0)
        lines.append(
            f"• {MODE_LABELS_RU.get(m0,'Режим')} режим отклонён: {_reason_to_user_text(r0, m0)} ({cat0})."
        )
    else:
        lines.append("• Сделка отклонена: нет надёжного сетапа по текущей структуре. (Structural).")

    if downgrade and len(modes_in_path) >= 2:
        m1 = normalize_mode(modes_in_path[1])
        lines.append(f"• Выполнен downgrade до {MODE_LABELS_RU_GEN.get(m1, 'другого')} режима.")

    if downgrade and len(rejected_steps) >= 2:
        second = rejected_steps[1]
        m1 = normalize_mode(second.get("mode"))
        r1 = str(second.get("reason") or "").strip()
        cat1 = classify_reason(r1)
        lines.append(
            f"• В {MODE_LABELS_RU_PREP.get(m1,'выбранном')} режиме сделка отклонена: {_reason_to_user_text(r1, m1)} ({cat1})."
        )

    reason_keys = []
    for s in rejected_steps:
        rk = str(s.get("reason") or "").strip()
        if rk:
            reason_keys.append(rk)

    needs = _what_must_change(reason_keys or [_infer_primary_no_trade_reason_key(d)], normalize_mode(d.get("mode")))

    lines.append("")
    lines.append("Что должно измениться:")
    lines.extend(needs)
    lines.append("")
    lines.append("Статус: рынок в фазе ожидания, бот продолжает мониторинг.")
    return "\n".join(lines)
