#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from macro_event_guard import evaluate_macro_event_guard, macro_dedupe_key, normalize_scheduled_macro_events
from channel_profiles import end_user_profiles, report_end_user_chat_ids

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGGER = logging.getLogger(__name__)
AGENT_STATE_REPO_ROOT = Path(os.getenv("STATE_GUARD_AGENT_REPO_ROOT", "/root/llm-signal-ai-agent"))
DEFAULT_STATE_GUARD_STATE_PATH = AGENT_STATE_REPO_ROOT / "logs/agent_trade_state.json"

DEFAULT_TARGET_CHAT_IDS = [-1003492385200, -1003493070625, -1003530482991]
DEFAULT_DIMA_CHAT_ID = -1003493070625
DEFAULT_START_DATE = "2026-06-05"
DEFAULT_SIGNAL_SLOTS = ["09:30", "12:30", "15:30", "18:30", "21:30", "00:30"]
SCHEDULER_UID = -9000605

IN_WORK_SIGNAL_STATUSES = {
    "WAIT_CONFIRM",
    "WAIT_POST_EVENT_REPRICE",
    "CONFIRM_LIVE",
    "SETUP_ARMED",
    "ENTRY_LIVE",
    "HOLD",
    "TIMEOUT_LIVE",
    "TP1_HIT_LIVE",
    "TP2_HIT_LIVE",
    "TRAIL_STOP",
    "REDUCE",
    "RUNNER_ACTIVE",
    "MANAGEMENT_ONLY",
}
WAIT_REPLACEABLE_STATUSES = {"WAIT_CONFIRM", "WAIT_POST_EVENT_REPRICE"}
LIVE_POSITION_STATUSES = {
    "ENTRY_LIVE",
    "HOLD",
    "TIMEOUT_LIVE",
    "TP1_HIT_LIVE",
    "TP2_HIT_LIVE",
    "TRAIL_STOP",
    "REDUCE",
    "RUNNER_ACTIVE",
    "PARTIALLY_REDUCED",
    "MANAGEMENT_ONLY",
}
SCHEDULED_POSITION_BLOCKING_STATUSES = {
    "ENTRY_LIVE",
    "HOLD",
    "TIMEOUT_LIVE",
    "TP1_HIT_LIVE",
    "TP2_HIT_LIVE",
    "REDUCE",
    "TRAIL_STOP",
    "RUNNER_ACTIVE",
    "RUNNER_ACTIVE_TO_TP3",
    "EXTEND_RUNNER",
    "EXTEND_TARGETS",
    "TRAIL_STOP_ACTIVE",
    "PARTIALLY_REDUCED",
    "MANAGEMENT_ONLY",
}
ACTIVE_SAME_DIRECTION_DUPLICATE_BLOCKING_STATUSES = {
    "WAIT_CONFIRM",
    "CONFIRM_LIVE",
    "SETUP_ARMED",
    "ENTRY_LIVE",
    "HOLD",
    "TIMEOUT_LIVE",
    "NEAR_TP1",
    "TP1_HIT_LIVE",
    "TP2_HIT_LIVE",
    "REDUCE",
    "TRAIL_STOP",
    "RUNNER_ACTIVE",
    "RUNNER_ACTIVE_TO_TP3",
    "EXTEND_RUNNER",
    "EXTEND_TARGETS",
    "TRAIL_STOP_ACTIVE",
    "PARTIALLY_REDUCED",
    "MANAGEMENT_ONLY",
}
SCHEDULED_PENDING_EXPIRING_STATUSES = {
    "WAIT_CONFIRM",
    "CONFIRM_LIVE",
    "SETUP_ARMED",
    "WAIT_POST_EVENT_REPRICE",
}
SCHEDULED_ADVISORY_NON_BLOCKING_STATUSES = {
    "RE_EVAL_ACTIVE_SIGNAL",
    "MARKET_REPRICE_ALERT",
    "ACTIVE_SIGNAL_UPDATE",
}
NOT_IN_WORK_SIGNAL_STATUSES = {
    "EXPIRED_NO_CONFIRM",
    "INVALIDATED_NO_CONFIRM",
    "CANCEL_WAIT_CONFIRM",
    "REPLACED_BY_NEW_SIGNAL",
    "CLOSED",
    "SL_HIT_LIVE",
    "STOP_LOSS_HIT",
    "TAKE_PROFIT_DONE",
}
SCHEDULED_SIGNAL_LIFECYCLE_EVENTS_PATH = LOGS_DIR / "scheduled_signal_lifecycle_events.jsonl"
STATE_GUARD_DECISION_LOG_FIELDS = (
    "state_guard_shadow_enabled",
    "state_guard_status",
    "state_guard_decision",
    "state_guard_decision_reason",
    "state_guard_can_publish_full_signal",
    "state_guard_recommended_publication_type",
    "state_guard_reason",
    "state_guard_primary_signal_id",
    "state_guard_primary_lifecycle_state",
    "state_guard_primary_position_status",
    "state_guard_primary_symbol",
    "state_guard_primary_direction",
    "state_guard_primary_entry",
    "state_guard_primary_sl",
    "state_guard_primary_tp1",
    "state_guard_primary_tp2",
    "state_guard_primary_tp3",
    "state_guard_active_same_direction_scenario_found",
    "state_guard_active_same_direction_signal_id",
    "state_guard_active_same_direction_status",
    "state_guard_active_same_direction_symbol",
    "state_guard_active_same_direction_direction",
    "state_guard_active_same_direction_lifecycle",
    "state_guard_duplicate_detected",
    "state_guard_conflict_detected",
    "state_guard_replacement_candidate",
    "state_guard_entry_distance_pct",
    "state_guard_sl_distance_pct",
    "state_guard_secondary_signal_ids",
    "state_guard_explanation",
    "state_guard_previous_signal_id",
    "state_guard_previous_outcome",
    "state_guard_previous_tp_reached",
    "state_guard_previous_runner_status",
    "state_guard_reentry_signal",
    "state_guard_reentry_allowed",
    "state_guard_reentry_reason",
    "state_guard_reentry_terminal_ttl_expired",
    "state_guard_reentry_allowed_by_time_sanity",
    "state_guard_reentry_terminal_elapsed_hours",
    "state_guard_reentry_terminal_ttl_hours",
    "state_guard_entry_blocked",
    "state_guard_entry_blocked_reason",
    "state_guard_old_signal_id",
    "state_guard_old_direction",
    "state_guard_new_bias_direction",
    "state_guard_previous_direction",
    "state_guard_new_direction",
    "state_guard_direction_match",
    "state_guard_regime_flip_candidate",
    "state_guard_regime_flip_reason",
    "state_guard_human_decision_required",
    "state_guard_old_setup_recommendation",
    "state_guard_propose_reverse_signal",
    "active_same_direction_scenario_found",
    "active_same_direction_signal_id",
    "active_same_direction_status",
    "active_same_direction_symbol",
    "active_same_direction_direction",
    "active_same_direction_lifecycle",
    "duplicate_detected",
    "duplicate_enforcement_enabled",
    "duplicate_enforcement_action",
    "state_guard_manual_override_enabled",
    "state_guard_manual_override_used",
    "state_guard_manual_override_reason",
    "state_guard_manual_override_source",
)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except Exception:
        return default


def scheduled_active_pending_max_age_hours() -> int:
    return max(1, parse_int_env("SCHEDULED_ACTIVE_PENDING_MAX_AGE_HOURS", 6))


def scheduled_wait_confirm_fallback_max_age_hours() -> int:
    # Keep scheduled duplicate-state filtering aligned with the AIA wait-confirm
    # deadline cap. Historical rows may miss confirm_timeout_minutes, so the
    # fallback must not expire a pending setup earlier than AIA lifecycle does.
    return max(1, parse_int_env("SCHEDULED_WAIT_CONFIRM_FALLBACK_MAX_AGE_HOURS", 6))


def scheduled_post_event_reprice_max_age_minutes() -> int:
    return max(1, parse_int_env("SCHEDULED_POST_EVENT_REPRICE_MAX_AGE_MINUTES", 90))


def scheduled_tp1_hit_without_runner_max_age_hours() -> int:
    return max(1, parse_int_env("SCHEDULED_TP1_HIT_WITHOUT_RUNNER_MAX_AGE_HOURS", 12))


def parse_date_env(name: str, default: str) -> date:
    raw = os.getenv(name, default)
    return date.fromisoformat(str(raw).strip())


def parse_chat_ids(raw: str | None = None) -> list[int]:
    raw = raw if raw is not None else os.getenv("SCHEDULED_PUBLISH_CHAT_IDS")
    if not raw:
        return DEFAULT_TARGET_CHAT_IDS.copy()
    out: list[int] = []
    seen: set[int] = set()
    for chunk in str(raw).replace(";", ",").split(","):
        text = chunk.strip()
        if not text or not text.lstrip("-").isdigit():
            continue
        chat_id = int(text)
        if chat_id not in seen:
            seen.add(chat_id)
            out.append(chat_id)
    return out or DEFAULT_TARGET_CHAT_IDS.copy()


def parse_hhmm(value: str) -> time:
    hour, minute = str(value).strip().split(":", 1)
    return time(int(hour), int(minute), tzinfo=MSK)


def parse_slots(raw: str | None = None) -> list[str]:
    raw = raw if raw is not None else os.getenv("SCHEDULED_SIGNAL_SLOTS_MSK")
    if not raw:
        return DEFAULT_SIGNAL_SLOTS.copy()
    slots = []
    for chunk in str(raw).replace(";", ",").split(","):
        text = chunk.strip()
        if text:
            parse_hhmm(text)
            slots.append(text)
    return slots or DEFAULT_SIGNAL_SLOTS.copy()


def to_msk(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(MSK).replace(microsecond=0)


def slot_datetime_msk(day: date, hhmm: str) -> datetime:
    t = parse_hhmm(hhmm)
    return datetime.combine(day, t, tzinfo=MSK).replace(microsecond=0)


def slot_id_for(slot_time_msk: datetime) -> str:
    return slot_time_msk.strftime("%Y%m%d_%H%M")


def mid_cycle_id(scheduled_date: date, start_date: date, interval_days: int) -> str:
    delta = (scheduled_date - start_date).days
    cycle_index = delta // interval_days
    return f"mid_{start_date.strftime('%Y%m%d')}_{cycle_index:04d}"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def emit_opposite_bias_before_entry(
    *,
    now_utc: datetime,
    old_signal_id: str,
    old_direction: str | None,
    candidate_signal_id: str | None,
    new_direction: str | None,
    symbol: str | None,
    recommendation: str,
    propose_reverse_signal: bool,
) -> dict:
    blocked_at = now_utc.isoformat().replace("+00:00", "Z")
    event = {
        "ts": blocked_at,
        "signal_id": old_signal_id,
        "symbol": symbol,
        "direction": old_direction,
        "status": "OPPOSITE_BIAS_BEFORE_ENTRY",
        "action_kind": "notify_opposite_bias_before_entry",
        "reason": "opposite_bias_before_entry",
        "entry_blocked": True,
        "entry_blocked_reason": "opposite_bias_before_entry",
        "blocked_at": blocked_at,
        "blocked_by_signal_id": candidate_signal_id,
        "old_signal_id": old_signal_id,
        "old_direction": old_direction,
        "new_bias_direction": new_direction,
        "human_decision_required": True,
        "recommended_publication_type": "urgent_review",
        "old_setup_recommendation": recommendation,
        "propose_reverse_signal": bool(propose_reverse_signal),
    }
    logs_dir = Path(os.getenv("AIA_LOGS_DIR", "/root/llm-signal-ai-agent/logs"))
    append_jsonl(logs_dir / f"agent_actions_{now_utc.strftime('%Y%m%d')}.jsonl", event)
    state_path = logs_dir / "market_watch_state.json"
    state = read_json(state_path)
    signals = state.setdefault("signals", {})
    current = signals.setdefault(old_signal_id, {})
    current.update(
        {
            "entry_blocked": True,
            "entry_blocked_reason": "opposite_bias_before_entry",
            "blocked_at": blocked_at,
            "blocked_by_signal_id": candidate_signal_id,
            "human_decision_required": True,
            "old_setup_recommendation": recommendation,
            "new_bias_direction": new_direction,
            "propose_reverse_signal": bool(propose_reverse_signal),
            "in_position": False,
        }
    )
    current.pop("entry_ts", None)
    write_json_atomic(state_path, state)
    return event


def relpath(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return str(path)


def _try_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalize_symbol(value) -> str:
    text = str(value or "").strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _display_symbol(value) -> str:
    text = str(value or "").strip().upper()
    if "/" in text:
        return text
    norm = _normalize_symbol(text)
    if norm.endswith("USDT") and len(norm) > 4:
        return f"{norm[:-4]}/USDT"
    return text or "UNKNOWN"


def _normalize_direction(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return text


def _confidence_rank(value) -> int | None:
    text = str(value or "").strip().lower()
    if text in {"low", "низкая"}:
        return 1
    if text in {"medium", "mid", "средняя"}:
        return 2
    if text in {"high", "высокая"}:
        return 3
    numeric = _try_float(value)
    if numeric is None:
        return None
    if numeric >= 0.75:
        return 3
    if numeric >= 0.5:
        return 2
    return 1


def _entry_from_payload(payload: dict) -> float | None:
    entry = _try_float(payload.get("entry_price"))
    if entry is not None:
        return entry
    entry_range = payload.get("entry_range") or payload.get("entry_zone")
    if isinstance(entry_range, dict):
        low = _try_float(entry_range.get("min"))
        high = _try_float(entry_range.get("max"))
    elif isinstance(entry_range, (list, tuple)) and len(entry_range) == 2:
        low = _try_float(entry_range[0])
        high = _try_float(entry_range[1])
    else:
        low = high = None
    if low is not None and high is not None:
        return (low + high) / 2.0
    return None


def _strong_opposite_candidate(candidate: dict) -> bool:
    score = _try_float(candidate.get("score") or candidate.get("confidence_score"))
    if score is not None:
        return score >= 0.75
    return (_confidence_rank(candidate.get("confidence")) or 0) >= 3


def _candidate_from_payload(payload: dict, signal_id: str | None = None) -> dict:
    tp = payload.get("tp") if isinstance(payload.get("tp"), dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "signal_id": signal_id,
        "symbol": _normalize_symbol(payload.get("symbol")),
        "display_symbol": _display_symbol(payload.get("symbol")),
        "direction": _normalize_direction(payload.get("direction") or payload.get("side")),
        "entry_price": _entry_from_payload(payload),
        "sl": _try_float(payload.get("sl")),
        "tp1": _try_float(payload.get("tp1") if payload.get("tp1") is not None else tp.get("tp1")),
        "tp2": _try_float(payload.get("tp2") if payload.get("tp2") is not None else tp.get("tp2")),
        "tp3": _try_float(payload.get("tp3") if payload.get("tp3") is not None else tp.get("tp3")),
        "rr": _try_float(payload.get("rr")),
        "required_risk_reward": _selected_mode_required_rr(
            str(payload.get("mode") or meta.get("mode") or ""), payload
        ),
        "confidence": payload.get("confidence"),
        "confidence_rank": _confidence_rank(payload.get("confidence")),
        "mode": str(payload.get("mode") or meta.get("mode") or "").strip().lower(),
        "holding_horizon": str(payload.get("holding_horizon") or "").strip().lower(),
        "strategy_type": str(payload.get("entry_mode") or meta.get("entry_type") or "").strip().lower(),
        "ema20_m15": _try_float(payload.get("ema20_m15")),
        "price_vs_ema20_m15": payload.get("price_vs_ema20_m15"),
        "price_vs_ema20_h1": payload.get("price_vs_ema20_h1"),
        "ema_guard_state": payload.get("ema_guard_state"),
        "ema_fan_m15_state": payload.get("ema_fan_m15_state"),
        "ema_fan_h1_state": payload.get("ema_fan_h1_state"),
        "confirmation_rules": payload.get("confirmation_rules"),
        "fresh_reclaim_present": payload.get("fresh_reclaim_present") or payload.get("fresh_reclaim") or payload.get("fresh_reset_reclaim"),
        "leader_status_retained": payload.get("leader_status_retained"),
        "why_asset": payload.get("why_asset"),
        "volume_confirmation_available": payload.get("volume_confirmation_available"),
        "volume_confirmation": payload.get("volume_confirmation"),
        "market_reaction": payload.get("market_reaction"),
        "structure_invalidated": payload.get("structure_invalidated"),
        "overextended_leader_risk": payload.get("overextended_leader_risk"),
        "chase_risk": payload.get("chase_risk") or (payload.get("flow_derivatives_modifiers") or {}).get("chase_risk") if isinstance(payload.get("flow_derivatives_modifiers"), dict) else payload.get("chase_risk"),
        "range_position": payload.get("range_position"),
        "directly_under_resistance": payload.get("directly_under_resistance"),
        "bnb_high_quality_eligible": payload.get("bnb_high_quality_eligible"),
        "_contract_candidate": True,
        "hard_block_conditions": payload.get("hard_block_conditions") if isinstance(payload.get("hard_block_conditions"), list) else [],
        "no_trade": bool(payload.get("no_trade")),
        "event_risk": payload.get("event_risk") if isinstance(payload.get("event_risk"), dict) else {},
        "explicit_regime_flip_reason": (
            payload.get("explicit_regime_flip_reason")
            or meta.get("explicit_regime_flip_reason")
        ),
    }


def _rr_for_signal(signal: dict) -> float | None:
    explicit = _try_float(signal.get("rr"))
    if explicit is not None:
        return explicit
    entry = _try_float(signal.get("entry_price"))
    sl = _try_float(signal.get("sl"))
    tp2 = _try_float(signal.get("tp2"))
    side = _normalize_direction(signal.get("direction"))
    if entry is None or sl is None or tp2 is None or entry == sl:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if side == "short":
        reward = entry - tp2
    else:
        reward = tp2 - entry
    return round(reward / risk, 4) if reward > 0 else None


FUNNEL_STAGE_PRE_GENERATION = "PRE_GENERATION"
FUNNEL_STAGE_NO_CANDIDATE = "NO_CANDIDATE"
FUNNEL_STAGE_CANDIDATE_SELECTED = "CANDIDATE_SELECTED"
FUNNEL_STAGE_CONTRACT_REJECTED = "CONTRACT_REJECTED"
FUNNEL_STAGE_POLICY_BLOCKED = "POLICY_BLOCKED"
FUNNEL_STAGE_WAIT_CONFIRM_PERSISTED = "WAIT_CONFIRM_PERSISTED"
FUNNEL_STAGE_FULL_SIGNAL_ELIGIBLE = "FULL_SIGNAL_ELIGIBLE"
FUNNEL_STAGE_WINDOW_ONLY = "WINDOW_ONLY"
FUNNEL_STAGE_PUBLICATION_SENT = "PUBLICATION_SENT"
FUNNEL_STAGE_PUBLICATION_SKIPPED = "PUBLICATION_SKIPPED"
FUNNEL_STAGE_RETRY_DEDUPED = "RETRY_DEDUPED"
FUNNEL_STAGE_RUN_CANCELLED = "RUN_CANCELLED"
FUNNEL_STAGE_RUN_FAILED = "RUN_FAILED"


def _clean_list(value) -> list:
    if value in (None, "", {}, []):
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item not in (None, "", [], {})]
    return [value]


def _first_present(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _selected_mode_required_rr(mode: str | None, payload: dict) -> float | None:
    for key in ("required_risk_reward", "required_rr", "min_rr_required", "rr_min_required"):
        value = _try_float(payload.get(key))
        if value is not None:
            return value
    mode = str(mode or payload.get("mode") or payload.get("requested_mode") or "").strip().lower()
    if mode == "aggressive":
        return 1.0
    if mode in {"neutral", "conservative"}:
        return 1.5
    return None


def _mode_rr(payload: dict, mode: str | None, candidate: dict) -> float | None:
    rr_by_mode = payload.get("rr_by_mode") if isinstance(payload.get("rr_by_mode"), dict) else {}
    mode_key = str(mode or payload.get("mode") or payload.get("requested_mode") or "").strip().lower()
    return _first_present(
        _try_float(rr_by_mode.get(mode_key)) if mode_key else None,
        _try_float(payload.get("risk_reward")),
        _rr_for_signal(candidate),
    )


def _conditions_to_eligibility(reasons: list) -> list[str]:
    mapping = {
        "counter_regime_long_requires_fresh_reclaim": "fresh_reclaim_required",
        "alt_long_conflicts_with_risk_off_requires_fresh_reclaim": "fresh_reclaim_required",
        "macro_event_direction_requires_fresh_reclaim": "fresh_reclaim_required",
        "counter_risk_without_regime_confirmation": "regime_confirmation_required",
        "blocked_by_severe_risk": "severe_risk_must_clear_or_policy_must_allow_confirm_only",
        "risk_reward_below_minimum": "risk_reward_must_reach_required_minimum",
        "confirmation_missing": "wait_confirm_rules_must_pass",
        "immediate_shock_active": "immediate_shock_window_must_expire",
        "immediate_shock_counter_risk": "immediate_shock_window_must_expire",
        "fresh_reclaim_missing": "fresh_reclaim_required",
        "regime_flip_missing": "regime_confirmation_required",
    }
    out: list[str] = []
    lowered = [str(reason).strip().lower() for reason in reasons if str(reason).strip()]
    concrete = [reason for reason in lowered if reason != "signal_core_no_trade"]
    for reason in concrete or lowered:
        condition = mapping.get(reason)
        if not condition and ("недостаточный rr" in reason or "risk_reward" in reason or "rr" in reason and "below" in reason):
            condition = "risk_reward_must_reach_required_minimum"
        if condition and condition not in out:
            out.append(condition)
    return out


def _candidate_funnel_base(stage: str = FUNNEL_STAGE_PRE_GENERATION) -> dict:
    return {
        "stage": stage,
        "candidate_generated": False,
        "candidate_count": 0,
        "considered_symbols": [],
        "considered_directions": [],
        "selected_symbol": None,
        "selected_direction": None,
        "entry_mode": None,
        "candidate_score": None,
        "structure_score": None,
        "confirmation_score": None,
        "confidence": None,
        "entry": None,
        "stop_loss": None,
        "take_profit": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "take_profit_3": None,
        "risk_reward": None,
        "required_risk_reward": None,
        "risk_reward_gap": None,
        "price_structure": None,
        "day_regime": None,
        "day_focus": [],
        "mid_regime": None,
        "m15_state": None,
        "h1_state": None,
        "ema20_m15_relation": None,
        "ema20_h1_relation": None,
        "ema_fan_m15": None,
        "ema_fan_h1": None,
        "adx_m15": None,
        "volume_confirmation": None,
        "fresh_reclaim_required": None,
        "fresh_reclaim_present": None,
        "regime_flip_required": None,
        "regime_flip_present": None,
        "confirmation_required": None,
        "confirmation_rules": [],
        "confirmation_deadline_minutes": None,
        "immediate_shock_active": None,
        "immediate_shock_until": None,
        "background_risk_active": None,
        "event_risk_level": None,
        "event_bias": None,
        "risk_size_mode": None,
        "execution_mode": None,
        "pre_generation_gate": None,
        "post_generation_gate": None,
        "full_signal_eligible": None,
        "full_signal_block_reasons": [],
        "no_trade_reasons": [],
        "publication_targets": [],
        "publication_result": None,
        "conditions_to_eligibility": [],
    }


def build_candidate_funnel(row: dict, cfg: SchedulerConfig, result: dict | None = None, *, stage: str | None = None) -> dict:
    result = result if isinstance(result, dict) else {}
    payload = result.get("last_payload") if isinstance(result.get("last_payload"), dict) else {}
    candidate = _candidate_from_payload(payload, signal_id=result.get("candidate_signal_id") or result.get("signal_id")) if payload else {}
    selected_mode = row.get("selected_mode") or result.get("selected_mode") or payload.get("mode") or payload.get("requested_mode")
    rr = _mode_rr(payload, selected_mode, candidate) if payload else _try_float(row.get("rr_new"))
    required_rr = _selected_mode_required_rr(str(selected_mode), payload) if payload else None
    rr_gap = round(rr - required_rr, 6) if rr is not None and required_rr is not None else None
    reasons = []
    reasons.extend(_clean_list(result.get("reason") or row.get("reason")))
    reasons.extend(_clean_list(result.get("blocked_reason") or row.get("blocked_reason")))
    reasons.extend(_clean_list(result.get("publication_type") if result.get("publication_type") == "blocked_by_severe_risk" else None))
    reasons.extend(_clean_list(payload.get("no_trade_reasons")))
    reasons.extend(_clean_list(payload.get("hard_block_conditions")))
    if rr is not None and required_rr is not None and rr < required_rr:
        reasons.append("risk_reward_below_minimum")

    final_stage = stage or _infer_candidate_funnel_stage(row, result, payload, rr, required_rr)
    funnel = _candidate_funnel_base(final_stage)
    if payload:
        symbol = candidate.get("display_symbol") or candidate.get("symbol")
        direction = candidate.get("direction")
        funnel.update(
            {
                "candidate_generated": True,
                "candidate_count": 1,
                "considered_symbols": _clean_list(payload.get("considered_symbols") or symbol),
                "considered_directions": _clean_list(payload.get("considered_directions") or direction),
                "selected_symbol": symbol,
                "selected_direction": direction,
                "entry_mode": payload.get("entry_mode") or candidate.get("strategy_type"),
                "candidate_score": _try_float(payload.get("candidate_score") or payload.get("score") or payload.get("confidence_score")),
                "structure_score": _try_float(payload.get("structure_score")),
                "confirmation_score": _try_float(payload.get("confirmation_score")),
                "confidence": payload.get("confidence"),
                "entry": _first_present(candidate.get("entry_price"), _try_float(payload.get("entry"))),
                "stop_loss": candidate.get("sl"),
                "take_profit": _first_present(candidate.get("tp2"), candidate.get("tp1"), candidate.get("tp3")),
                "take_profit_1": candidate.get("tp1"),
                "take_profit_2": candidate.get("tp2"),
                "take_profit_3": candidate.get("tp3"),
                "risk_reward": rr,
                "required_risk_reward": required_rr,
                "risk_reward_gap": rr_gap,
                "price_structure": _first_present(payload.get("price_structure"), payload.get("ema_guard_state")),
                "day_regime": _first_present(payload.get("day_regime"), row.get("day_regime")),
                "day_focus": _clean_list(payload.get("day_focus") or row.get("day_focus")),
                "mid_regime": payload.get("mid_regime"),
                "m15_state": _first_present(payload.get("m15_state"), payload.get("price_vs_ema20_m15")),
                "h1_state": _first_present(payload.get("h1_state"), payload.get("price_vs_ema20_h1")),
                "ema20_m15_relation": payload.get("price_vs_ema20_m15"),
                "ema20_h1_relation": payload.get("price_vs_ema20_h1"),
                "ema_fan_m15": _first_present(payload.get("ema_fan_m15"), payload.get("ema_fan_m15_state")),
                "ema_fan_h1": _first_present(payload.get("ema_fan_h1"), payload.get("ema_fan_h1_state")),
                "adx_m15": _first_present(payload.get("adx_m15"), (payload.get("adx_guard") or {}).get("state") if isinstance(payload.get("adx_guard"), dict) else payload.get("adx_guard")),
                "volume_confirmation": payload.get("volume_confirmation"),
                "fresh_reclaim_required": any("fresh_reclaim" in str(reason) for reason in reasons),
                "fresh_reclaim_present": _first_present(payload.get("fresh_reclaim_present"), payload.get("fresh_reclaim"), payload.get("fresh_reset_reclaim"), payload.get("fresh_higher_low_after_reset")),
                "regime_flip_required": any("regime_confirmation" in str(reason) or "regime_flip" in str(reason) for reason in reasons),
                "regime_flip_present": bool(_first_present(payload.get("explicit_regime_flip_reason"), payload.get("regime_flip_reason"), result.get("explicit_regime_flip_reason"))),
                "confirmation_required": _first_present(result.get("confirmation_required"), payload.get("entry_mode") == "wait_confirm", row.get("entry_mode_required") == "WAIT_CONFIRM"),
                "confirmation_rules": _clean_list(payload.get("confirmation_rules")),
                "confirmation_deadline_minutes": _try_float(_first_present(payload.get("confirm_timeout_minutes"), payload.get("validity_minutes"), payload.get("max_valid_minutes"))),
            }
        )
    else:
        funnel["candidate_count"] = 0

    targets = _publication_target_names(cfg, result)
    full_signal_eligible = _first_present(result.get("can_publish_full_signal"), result.get("state_guard_can_publish_full_signal"))
    if result.get("published") and str(result.get("publication_type") or "full_signal") == "full_signal":
        full_signal_eligible = True
    funnel.update(
        {
            "immediate_shock_active": _first_present(result.get("immediate_shock_window"), row.get("immediate_shock_window")),
            "immediate_shock_until": _first_present(result.get("immediate_shock_until"), row.get("immediate_shock_until")),
            "background_risk_active": _first_present(result.get("background_risk_active"), row.get("background_risk_active")),
            "event_risk_level": _first_present(result.get("event_risk_level"), row.get("event_risk_level"), payload.get("event_risk_level")),
            "event_bias": _first_present(result.get("event_bias"), row.get("event_bias"), payload.get("event_bias")),
            "risk_size_mode": _first_present(result.get("risk_size_mode"), row.get("risk_size_mode")),
            "execution_mode": _first_present(result.get("execution_mode"), row.get("execution_mode")),
            "pre_generation_gate": "allowed" if row.get("hard_block_reasons") in ([], None) and row.get("decision") not in {"cancel", "defer"} else row.get("reason"),
            "post_generation_gate": _first_present(result.get("publication_type"), result.get("post_generation_event_risk_blocked_reason"), result.get("blocked_reason")),
            "full_signal_eligible": full_signal_eligible,
            "full_signal_block_reasons": list(dict.fromkeys([str(reason) for reason in reasons if str(reason) not in {"published", "no_valid_signal_candidate"}])),
            "no_trade_reasons": _clean_list(payload.get("no_trade_reasons")),
            "publication_targets": targets,
            "publication_result": _publication_result_label(row, result),
        }
    )
    funnel["conditions_to_eligibility"] = _conditions_to_eligibility(funnel["full_signal_block_reasons"])
    return funnel


def _infer_candidate_funnel_stage(row: dict, result: dict, payload: dict, rr: float | None, required_rr: float | None) -> str:
    decision = str(row.get("decision") or "").strip().lower()
    reason = str(result.get("reason") or row.get("reason") or "").strip()
    publication_type = str(result.get("publication_type") or row.get("publication_type") or "").strip().lower()
    if decision == "duplicate_skip":
        return FUNNEL_STAGE_RETRY_DEDUPED
    if decision == "error":
        return FUNNEL_STAGE_RUN_FAILED
    if reason == "no_valid_signal_candidate":
        return FUNNEL_STAGE_NO_CANDIDATE
    if decision in {"cancel", "skipped_disabled"} and not payload:
        return FUNNEL_STAGE_RUN_CANCELLED
    if result.get("published"):
        if publication_type in {"full_signal", "tactical_confirm_signal"}:
            return FUNNEL_STAGE_PUBLICATION_SENT
        return FUNNEL_STAGE_PUBLICATION_SKIPPED
    if payload and (payload.get("no_trade") or reason == "signal_core_no_trade"):
        if rr is not None and required_rr is not None and rr < required_rr:
            return FUNNEL_STAGE_CONTRACT_REJECTED
        no_trade = " ".join(str(item).lower() for item in _clean_list(payload.get("no_trade_reasons")) + _clean_list(payload.get("no_trade_hint")))
        if "rr" in no_trade or "уров" in no_trade or "level" in no_trade:
            return FUNNEL_STAGE_CONTRACT_REJECTED
        return FUNNEL_STAGE_NO_CANDIDATE
    if publication_type == "blocked_by_severe_risk" or result.get("can_publish_full_signal") is False:
        return FUNNEL_STAGE_POLICY_BLOCKED
    if payload:
        if result.get("aia_forward_attempted") and _candidate_entry_mode(_candidate_from_payload(payload)) == "wait_confirm":
            return FUNNEL_STAGE_WAIT_CONFIRM_PERSISTED
        return FUNNEL_STAGE_FULL_SIGNAL_ELIGIBLE
    if row.get("window_message_sent"):
        return FUNNEL_STAGE_WINDOW_ONLY
    return FUNNEL_STAGE_PRE_GENERATION


def _logical_target_name(chat_id: int, cfg: SchedulerConfig) -> str | None:
    dima_chat_id = int(getattr(cfg, "dima_chat_id", DEFAULT_DIMA_CHAT_ID))
    if int(chat_id) == dima_chat_id:
        return "Dima"
    non_dima = [item for item in cfg.target_chat_ids if int(item) != dima_chat_id]
    if non_dima and int(chat_id) == int(non_dima[0]):
        return "Sergey"
    if len(non_dima) > 1 and int(chat_id) == int(non_dima[1]):
        return "mixed"
    return None


def _publication_target_names(cfg: SchedulerConfig, result: dict | None = None) -> list[str]:
    result = result if isinstance(result, dict) else {}
    raw_targets = result.get("scheduled_full_signal_targets")
    targets = raw_targets if isinstance(raw_targets, list) else scheduled_full_signal_targets(cfg)
    names: list[str] = []
    for chat_id in targets:
        name = _logical_target_name(int(chat_id), cfg)
        if name and name not in names:
            names.append(name)
    return names


def _publication_result_label(row: dict, result: dict) -> str | None:
    if result.get("published"):
        return "sent"
    if result:
        return "skipped"
    if row.get("window_message_sent"):
        return "sent"
    if row.get("dima_window_cooldown_applied"):
        return "skipped"
    return None


def build_publication_audit(row: dict, cfg: SchedulerConfig, result: dict | None = None) -> dict:
    result = result if isinstance(result, dict) else {}
    publication_type = str(result.get("publication_type") or row.get("publication_type") or "").strip().lower()
    payload_type = None
    if row.get("window_message_sent") or row.get("dima_window_cooldown_applied"):
        payload_type = "WINDOW_ONLY"
    if publication_type in {"full_signal", "tactical_confirm_signal"} or (result.get("published") and not publication_type):
        payload_type = "FULL_SIGNAL"
    elif publication_type in {"active_signal_update", "management_update", "conflict_update", "urgent_review", "replace_wait_confirm", "suppress_duplicate"}:
        payload_type = "LIFECYCLE_NOTIFICATION"
    audit = {
        "payload_type": payload_type,
        "targets": {
            "Dima": {"expected": False, "created": False, "sender_seen": None, "sent": False, "skipped": False, "reason": None},
            "Sergey": {"expected": False, "created": False, "sender_seen": None, "sent": False, "skipped": False, "reason": None},
            "mixed": {"expected": False, "created": False, "sender_seen": None, "sent": False, "skipped": False, "reason": None},
        },
    }
    if payload_type == "WINDOW_ONLY":
        target = audit["targets"]["Dima"]
        target.update(
            {
                "expected": dima_window_only(cfg),
                "created": bool(row.get("window_message_sent") or row.get("dima_window_cooldown_applied")),
                "sender_seen": True if row.get("window_message_sent") else None,
                "sent": bool(row.get("window_message_sent")),
                "skipped": bool(row.get("dima_window_cooldown_applied")),
                "reason": row.get("dima_window_audit_reason") or row.get("dima_window_update_reason"),
            }
        )
        return audit
    expected_names = _publication_target_names(cfg, result)
    for name in expected_names:
        target = audit["targets"][name]
        target["expected"] = True
        target["created"] = bool(result)
        target["sender_seen"] = True if result.get("published") and payload_type == "FULL_SIGNAL" else None
        target["sent"] = bool(result.get("published") and payload_type == "FULL_SIGNAL")
        target["skipped"] = bool(result and not result.get("published"))
        target["reason"] = result.get("reason")
    if dima_window_only(cfg) and payload_type == "FULL_SIGNAL":
        audit["targets"]["Dima"].update({"expected": False, "created": False, "sent": False, "skipped": True, "reason": "dima_window_only"})
    return audit


def build_wait_confirm_lifecycle_audit(row: dict, result: dict | None = None) -> dict:
    result = result if isinstance(result, dict) else {}
    payload = result.get("last_payload") if isinstance(result.get("last_payload"), dict) else {}
    entry_mode = str(payload.get("entry_mode") or "").strip().lower()
    attempted = bool(result.get("aia_forward_attempted")) if entry_mode == "wait_confirm" else False
    return {
        "lifecycle_handoff_attempted": attempted,
        "lifecycle_record_created": bool(result.get("lifecycle_created")) if result.get("lifecycle_created") is not None else False,
        "confirm_rule_version": payload.get("confirm_profile_used"),
        "confirmation_rules": _clean_list(payload.get("confirmation_rules")),
        "confirmation_deadline_minutes": _try_float(_first_present(payload.get("confirm_timeout_minutes"), payload.get("validity_minutes"), payload.get("max_valid_minutes"))),
        "watcher_visible_id": result.get("signal_id") or result.get("candidate_signal_id"),
        "aia_ingest_occurred": result.get("aia_forward_ok") if attempted else None,
        "async_market_watch_followup_required": True if attempted and result.get("aia_forward_ok") else None,
        "observed_lifecycle_state": None,
    }


def add_scheduled_decision_observability(row: dict, cfg: SchedulerConfig, result: dict | None = None, *, stage: str | None = None) -> dict:
    row["candidate_funnel"] = build_candidate_funnel(row, cfg, result, stage=stage)
    row["publication_audit"] = build_publication_audit(row, cfg, result)
    row["wait_confirm_lifecycle_audit"] = build_wait_confirm_lifecycle_audit(row, result)
    return row


def _distance_pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    base = max(abs(a), abs(b))
    if base <= 0:
        return None
    return abs(a - b) / base * 100.0


def _strategy_compatible(old: dict, new: dict) -> bool:
    old_strategy = str(old.get("strategy_type") or "").strip().lower()
    new_strategy = str(new.get("strategy_type") or "").strip().lower()
    if old_strategy and new_strategy and old_strategy != new_strategy:
        return False
    old_mode = str(old.get("mode") or "").strip().lower()
    new_mode = str(new.get("mode") or "").strip().lower()
    execution_modes = {"aggressive", "neutral", "conservative"}
    if old_mode in execution_modes and new_mode in execution_modes:
        return True
    return not (old_mode and new_mode and old_mode != new_mode)


def _primary_sort_key(signal: dict) -> tuple[int, float, str]:
    status = str(signal.get("status") or "").strip().upper()
    priority = 0
    if status in LIVE_POSITION_STATUSES:
        priority = 4
    elif status in {"CONFIRM_LIVE", "SETUP_ARMED"}:
        priority = 3
    elif status in {"WAIT_CONFIRM", "WAIT_POST_EVENT_REPRICE"}:
        priority = 2
    elif status in IN_WORK_SIGNAL_STATUSES:
        priority = 1
    event_time = _signal_event_time(signal)
    event_ts = event_time.timestamp() if event_time is not None else float("-inf")
    return (priority, event_ts, str(signal.get("signal_id") or ""))


def _tp_ladder_validation(
    direction: str | None,
    entry: float | None,
    tp1: float | None,
    tp2: float | None,
    tp3: float | None,
) -> dict:
    direction = _normalize_direction(direction)
    warnings: list[str] = []
    invalid = False
    duplicate_targets = False

    def _append(condition: bool, label: str) -> None:
        nonlocal invalid
        if condition:
            warnings.append(label)
            invalid = True

    if direction == "long":
        _append(entry is not None and tp1 is not None and tp1 <= entry, "tp1_not_above_entry")
        _append(tp1 is not None and tp2 is not None and tp2 < tp1, "tp2_below_tp1")
        _append(tp2 is not None and tp3 is not None and tp3 < tp2, "tp3_below_tp2")
    elif direction == "short":
        _append(entry is not None and tp1 is not None and tp1 >= entry, "tp1_not_below_entry")
        _append(tp1 is not None and tp2 is not None and tp2 > tp1, "tp2_above_tp1")
        _append(tp2 is not None and tp3 is not None and tp3 > tp2, "tp3_above_tp2")

    if tp1 is not None and tp2 is not None and tp1 == tp2:
        warnings.append("tp1_equals_tp2")
        duplicate_targets = True
        invalid = True
    if tp2 is not None and tp3 is not None and tp2 == tp3:
        warnings.append("tp2_equals_tp3")
        duplicate_targets = True
        invalid = True

    return {
        "invalid_tp_ladder": invalid,
        "duplicate_tp_targets": duplicate_targets,
        "tp_ladder_warnings": warnings,
    }


def _horizon_compatible(old: dict, new: dict) -> bool:
    old_horizon = str(old.get("holding_horizon") or "").strip().lower()
    new_horizon = str(new.get("holding_horizon") or "").strip().lower()
    if not old_horizon or not new_horizon:
        return True
    intraday_family = {"intraday", "intraday_to_1_2d"}
    swing_family = {"short_swing", "multi_day"}
    return old_horizon == new_horizon or {old_horizon, new_horizon} <= intraday_family or {old_horizon, new_horizon} <= swing_family


def _parse_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        pass
    return rows


def _parse_signal_datetime(value) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def _signal_event_time(signal: dict) -> datetime | None:
    parsed = [
        _parse_signal_datetime(signal.get(key))
        for key in (
            "last_event_at",
            "last_event_at_utc",
            "updated_at",
            "updated_at_utc",
            "created_at",
            "created_at_utc",
            "published_at",
            "published_at_utc",
            "timestamp",
            "timestamp_utc",
            "ts",
            "ts_utc",
        )
    ]
    times = [dt for dt in parsed if dt is not None]
    return max(times) if times else None


def _signal_id_date(signal_id: str | None) -> date | None:
    text = str(signal_id or "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except Exception:
        return None


def _signal_position_status(signal: dict) -> str:
    return str(signal.get("position_status") or signal.get("primary_position_status") or "").strip().upper()


def _signal_runner_or_open_position_known(signal: dict) -> bool:
    position_status = _signal_position_status(signal)
    return (
        position_status in {"OPEN", "RUNNER_ACTIVE"}
        or bool(signal.get("runner_active"))
        or bool(signal.get("runner_open"))
        or bool(signal.get("open_position"))
        or bool(signal.get("position_open"))
    )


def _signal_has_open_position(signal: dict) -> bool:
    status = str(signal.get("status") or "").strip().upper()
    position_status = _signal_position_status(signal)
    explicit_open = _signal_runner_or_open_position_known(signal)
    if status == "TP1_HIT_LIVE":
        return explicit_open
    return (
        explicit_open
        or status in SCHEDULED_POSITION_BLOCKING_STATUSES
        or (status == "TIMEOUT_LIVE" and explicit_open)
        or (status == "TP2_HIT_LIVE" and explicit_open)
    )


def _stale_active_signal_defaults() -> dict:
    return {
        "stale_active_signal_ignored": False,
        "stale_active_signal_id": None,
        "stale_active_signal_status": None,
        "stale_active_signal_age_hours": None,
        "stale_active_signal_reason": None,
        "stale_tp1_hit_signal_ignored": False,
        "stale_tp1_hit_signal_id": None,
        "stale_tp1_hit_signal_age_hours": None,
        "stale_tp1_hit_signal_reason": None,
    }


def _stale_active_signal_result(signal: dict, *, now_utc: datetime | None = None) -> dict:
    now_utc = (now_utc or utc_now()).astimezone(UTC).replace(microsecond=0)
    status = str(signal.get("status") or "").strip().upper()
    signal_id = str(signal.get("signal_id") or "") or None
    out = _stale_active_signal_defaults()
    out.update({"stale_active_signal_id": signal_id, "stale_active_signal_status": status or None})

    if not status:
        return out
    if _signal_has_open_position(signal):
        return out
    if status == "TP1_HIT_LIVE":
        event_time = _signal_event_time(signal)
        if event_time is not None:
            age_hours = (now_utc - event_time).total_seconds() / 3600.0
            out["stale_active_signal_age_hours"] = round(max(0.0, age_hours), 3)
            if age_hours > scheduled_tp1_hit_without_runner_max_age_hours():
                out.update(
                    {
                        "stale_active_signal_ignored": True,
                        "stale_active_signal_reason": "tp1_hit_without_runner_expired",
                        "stale_tp1_hit_signal_ignored": True,
                        "stale_tp1_hit_signal_id": signal_id,
                        "stale_tp1_hit_signal_age_hours": round(max(0.0, age_hours), 3),
                        "stale_tp1_hit_signal_reason": "tp1_hit_without_runner_expired",
                    }
                )
        return out
    if status in SCHEDULED_ADVISORY_NON_BLOCKING_STATUSES:
        out.update({"stale_active_signal_ignored": True, "stale_active_signal_reason": "advisory_state_non_blocking"})
        return out
    if status not in SCHEDULED_PENDING_EXPIRING_STATUSES:
        return out

    event_time = _signal_event_time(signal)
    valid_until = _parse_signal_datetime(signal.get("valid_until") or signal.get("valid_until_utc") or signal.get("wait_confirm_valid_until"))
    if status == "WAIT_CONFIRM" and valid_until is None:
        timeout_minutes = _try_float(signal.get("confirm_timeout_minutes") or signal.get("wait_confirm_timeout_minutes"))
        if timeout_minutes is not None and event_time is not None:
            valid_until = event_time + timedelta(minutes=max(1.0, timeout_minutes))

    if status == "WAIT_CONFIRM" and valid_until is not None:
        age_hours = (now_utc - valid_until).total_seconds() / 3600.0
        out["stale_active_signal_age_hours"] = round(max(0.0, age_hours), 3)
        if now_utc > valid_until:
            out.update({"stale_active_signal_ignored": True, "stale_active_signal_reason": "wait_confirm_valid_until_expired"})
        return out

    max_age_hours = scheduled_active_pending_max_age_hours()
    if status == "WAIT_CONFIRM":
        max_age_hours = scheduled_wait_confirm_fallback_max_age_hours()
    elif status == "WAIT_POST_EVENT_REPRICE":
        max_age_hours = scheduled_post_event_reprice_max_age_minutes() / 60.0

    if event_time is not None:
        age_hours = (now_utc - event_time).total_seconds() / 3600.0
        out["stale_active_signal_age_hours"] = round(max(0.0, age_hours), 3)
        if age_hours > max_age_hours:
            out.update({"stale_active_signal_ignored": True, "stale_active_signal_reason": "pending_state_ttl_expired"})
        return out

    signal_day = _signal_id_date(signal_id)
    if signal_day is not None and (to_msk(now_utc).date() - signal_day).days > 1:
        out.update({"stale_active_signal_ignored": True, "stale_active_signal_reason": "signal_id_date_expired"})
    return out


def _recent_aia_log_paths(now_utc: datetime | None = None, days: int = 7) -> list[Path]:
    root = Path(os.getenv("AIA_LOGS_DIR", "/root/llm-signal-ai-agent/logs"))
    if now_utc is None:
        now_utc = utc_now()
    out: list[Path] = []
    for offset in range(days - 1, -1, -1):
        tag = (now_utc.date() - timedelta(days=offset)).strftime("%Y%m%d")
        out.append(root / f"agent_actions_{tag}.jsonl")
    return out


def _action_row_to_signal(row: dict) -> dict | None:
    signal_id = row.get("signal_id")
    status = str(row.get("status") or "").strip().upper()
    symbol = _normalize_symbol(row.get("symbol"))
    direction = _normalize_direction(row.get("direction"))
    if not signal_id or not status:
        return None
    event_risk = row.get("event_risk_context") if isinstance(row.get("event_risk_context"), dict) else {}
    return {
        "signal_id": str(signal_id),
        "status": status,
        "symbol": symbol,
        "display_symbol": _display_symbol(symbol),
        "direction": direction,
        "entry_price": _try_float(row.get("entry_price")),
        "sl": _try_float(row.get("sl")),
        "tp1": _try_float(row.get("tp1")),
        "tp2": _try_float(row.get("tp2")),
        "tp3": _try_float(row.get("tp3")),
        "rr": _try_float(row.get("rr")),
        "confidence": row.get("confidence"),
        "confidence_rank": _confidence_rank(row.get("confidence")),
        "mode": str(row.get("mode") or "").strip().lower(),
        "holding_horizon": str(row.get("holding_horizon") or "").strip().lower(),
        "strategy_type": str(row.get("entry_mode") or "").strip().lower(),
        "ts": row.get("ts") or row.get("ts_utc"),
        "created_at": row.get("created_at"),
        "published_at": row.get("published_at"),
        "last_event_at": row.get("last_event_at") or row.get("updated_at"),
        "valid_until": row.get("valid_until"),
        "confirm_timeout_minutes": row.get("confirm_timeout_minutes"),
        "position_status": row.get("position_status"),
        "runner_active": row.get("runner_active"),
        "runner_status": row.get("runner_status"),
        "filled": status in LIVE_POSITION_STATUSES or bool(row.get("entry_detection_ts")),
        "cancelled_by_scheduler": False,
        "hard_block_conditions": row.get("hard_block_conditions") if isinstance(row.get("hard_block_conditions"), list) else [],
        "event_risk_level": row.get("event_risk_level") or event_risk.get("event_risk_level"),
    }


def load_in_work_signal_state(now_utc: datetime | None = None) -> list[dict]:
    latest: dict[str, dict] = {}
    for path in _recent_aia_log_paths(now_utc):
        for row in _parse_jsonl(path):
            signal = _action_row_to_signal(row)
            if signal is not None:
                latest[signal["signal_id"]] = {**latest.get(signal["signal_id"], {}), **signal}

    for row in _parse_jsonl(SCHEDULED_SIGNAL_LIFECYCLE_EVENTS_PATH):
        signal_id = str(row.get("signal_id") or row.get("replaced_signal_id") or "")
        if not signal_id:
            continue
        current = latest.get(signal_id, {"signal_id": signal_id})
        status = str(row.get("status") or "").strip().upper()
        if status:
            current["status"] = status
        if row.get("ts") or row.get("ts_utc"):
            current["last_event_at"] = row.get("ts") or row.get("ts_utc")
        if row.get("position_status"):
            current["position_status"] = row.get("position_status")
        current["cancelled_by_scheduler"] = status in {"CANCEL_WAIT_CONFIRM", "REPLACED_BY_NEW_SIGNAL"}
        current["replaced_by_signal_id"] = row.get("replaced_by_signal_id")
        latest[signal_id] = current

    return [
        signal
        for signal in latest.values()
        if (
            str(signal.get("status") or "").upper() in IN_WORK_SIGNAL_STATUSES
            or _signal_position_status(signal) == "OPEN"
        )
        and not signal.get("cancelled_by_scheduler")
    ]


def evaluate_duplicate_publication(candidate: dict, active_signals: list[dict], *, now_utc: datetime | None = None) -> dict:
    out = {
        "duplicate_in_work_signal_detected": False,
        "duplicate_signal_id": None,
        "duplicate_signal_status": None,
        "publication_type": "full_signal",
        "replacement_selected": False,
        "replacement_reason": "",
        "replaced_signal_id": None,
        "replacing_signal_id": candidate.get("signal_id"),
        "old_entry": None,
        "new_candidate_entry": candidate.get("entry_price"),
        "old_sl": None,
        "new_candidate_sl": candidate.get("sl"),
        "entry_distance_pct": None,
        "sl_distance_pct": None,
        "rr_old": None,
        "rr_new": _rr_for_signal(candidate),
        "confidence_old": None,
        "confidence_new": candidate.get("confidence"),
        "strategy_same_or_compatible": True,
        "duplicate_signal": None,
        "headline_risk_delta": _event_risk_field({}, candidate, "headline_risk_delta") or "NONE",
        "previous_risk_level": _event_risk_field({}, candidate, "previous_risk_level"),
        "new_risk_level": _event_risk_field({}, candidate, "new_risk_level"),
        "risk_transition_reason": _event_risk_field({}, candidate, "risk_transition_reason"),
        "duplicate_decision": _event_risk_field({}, candidate, "duplicate_decision") or "duplicate_headline_same_state",
        "headline_update_generated": bool(_event_risk_field({}, candidate, "headline_update_generated")),
        "confirmation_required": False,
        "confirmation_required_reason": None,
        "management_review_required": False,
        "management_review_reason": None,
        "risk_reassessment_required": False,
        "risk_reassessment_reason": None,
        **_stale_active_signal_defaults(),
    }
    symbol = candidate.get("symbol")
    direction = candidate.get("direction")
    same_symbol = []
    for signal in active_signals:
        if str(signal.get("status") or "").strip().upper() in NOT_IN_WORK_SIGNAL_STATUSES:
            continue
        if signal.get("symbol") != symbol:
            continue
        if now_utc is None:
            same_symbol.append(signal)
            continue
        stale_result = _stale_active_signal_result(signal, now_utc=now_utc)
        if stale_result.get("stale_active_signal_ignored"):
            if not out["stale_active_signal_ignored"]:
                out.update(stale_result)
            LOGGER.info(
                "stale active signal ignored",
                extra={
                    "stale_active_signal_id": stale_result.get("stale_active_signal_id"),
                    "stale_active_signal_status": stale_result.get("stale_active_signal_status"),
                    "stale_active_signal_age_hours": stale_result.get("stale_active_signal_age_hours"),
                    "stale_active_signal_reason": stale_result.get("stale_active_signal_reason"),
                },
            )
            continue
        same_symbol.append(signal)
    opposite = next((s for s in same_symbol if s.get("direction") and s.get("direction") != direction), None)
    if opposite is not None:
        opposite_status = str(opposite.get("status") or "").strip().upper()
        pre_entry = (
            opposite_status in {"WAIT_CONFIRM", "CONFIRM_LIVE", "SETUP_ARMED"}
            and not bool(opposite.get("filled"))
            and str(opposite.get("position_status") or "").strip().upper() in {"", "NONE"}
        )
        strong_opposite = _strong_opposite_candidate(candidate)
        out.update(
            {
                "duplicate_in_work_signal_detected": True,
                "duplicate_signal_id": opposite.get("signal_id"),
                "duplicate_signal_status": opposite.get("status"),
                "publication_type": "urgent_review" if pre_entry else "conflict_update",
                "replacement_reason": (
                    "opposite_bias_before_entry" if pre_entry else "opposite_direction_in_work_signal"
                ),
                "duplicate_signal": opposite,
                "entry_blocked": pre_entry,
                "entry_blocked_reason": "opposite_bias_before_entry" if pre_entry else None,
                "human_decision_required": pre_entry,
                "old_setup_recommendation": (
                    "CANCEL_ARMED_SETUP" if pre_entry and strong_opposite else "WAIT_HUMAN_DECISION"
                    if pre_entry
                    else None
                ),
                "propose_reverse_signal": bool(pre_entry and strong_opposite),
            }
        )
        _apply_headline_escalation_setup_impact(out, opposite_status)
        return out

    entry_threshold = _try_float(os.getenv("SCHEDULED_DUPLICATE_ENTRY_DISTANCE_PCT")) or 0.5
    sl_threshold = _try_float(os.getenv("SCHEDULED_DUPLICATE_SL_DISTANCE_PCT")) or 1.0
    related: list[tuple[float, dict, float | None, float | None, bool]] = []
    same_direction_candidates: list[tuple[dict, float | None, float | None, bool]] = []
    for old in same_symbol:
        if old.get("direction") != direction:
            continue
        entry_distance = _distance_pct(old.get("entry_price"), candidate.get("entry_price"))
        sl_distance = _distance_pct(old.get("sl"), candidate.get("sl"))
        strategy_ok = _strategy_compatible(old, candidate)
        horizon_ok = _horizon_compatible(old, candidate)
        if strategy_ok and horizon_ok:
            same_direction_candidates.append((old, entry_distance, sl_distance, strategy_ok))
        if (
            entry_distance is not None
            and entry_distance <= entry_threshold
            and (sl_distance is None or sl_distance <= sl_threshold)
            and strategy_ok
            and horizon_ok
        ):
            related.append((entry_distance, old, entry_distance, sl_distance, strategy_ok))

    if not related and same_direction_candidates:
        old, entry_distance, sl_distance, strategy_ok = max(
            same_direction_candidates,
            key=lambda item: _primary_sort_key(item[0]),
        )
        rr_old = _rr_for_signal(old)
        rr_new = _rr_for_signal(candidate)
        out.update(
            {
                "duplicate_in_work_signal_detected": True,
                "duplicate_signal_id": old.get("signal_id"),
                "duplicate_signal_status": old.get("status"),
                "publication_type": "active_signal_update",
                "replacement_reason": "same_symbol_direction_in_work_material_reprice",
                "replaced_signal_id": None,
                "old_entry": old.get("entry_price"),
                "old_sl": old.get("sl"),
                "entry_distance_pct": round(entry_distance, 6) if entry_distance is not None else None,
                "sl_distance_pct": round(sl_distance, 6) if sl_distance is not None else None,
                "rr_old": rr_old,
                "rr_new": rr_new,
                "confidence_old": old.get("confidence"),
                "strategy_same_or_compatible": strategy_ok,
                "duplicate_signal": old,
            }
        )
        status = str(old.get("status") or "").upper()
        if status in LIVE_POSITION_STATUSES:
            out["replacement_reason"] = "existing_signal_live_or_management"
        elif status in {"CONFIRM_LIVE", "SETUP_ARMED"}:
            out["replacement_reason"] = "existing_signal_confirmed_or_armed"
        elif status in WAIT_REPLACEABLE_STATUSES:
            out["replacement_reason"] = "wait_confirm_exists_material_reprice"
        _apply_headline_escalation_setup_impact(out, status)
        return out
    if not related:
        return out

    _, old, entry_distance, sl_distance, strategy_ok = sorted(related, key=lambda item: item[0])[0]
    rr_old = _rr_for_signal(old)
    rr_new = _rr_for_signal(candidate)
    out.update(
        {
            "duplicate_in_work_signal_detected": True,
            "duplicate_signal_id": old.get("signal_id"),
            "duplicate_signal_status": old.get("status"),
            "publication_type": "active_signal_update",
            "replacement_reason": "same_symbol_direction_in_work",
            "replaced_signal_id": None,
            "old_entry": old.get("entry_price"),
            "old_sl": old.get("sl"),
            "entry_distance_pct": round(entry_distance, 6) if entry_distance is not None else None,
            "sl_distance_pct": round(sl_distance, 6) if sl_distance is not None else None,
            "rr_old": rr_old,
            "rr_new": rr_new,
            "confidence_old": old.get("confidence"),
            "strategy_same_or_compatible": strategy_ok,
            "duplicate_signal": old,
        }
    )

    status = str(old.get("status") or "").upper()
    if status in LIVE_POSITION_STATUSES:
        out["publication_type"] = "active_signal_update"
        out["replacement_reason"] = "existing_signal_live_or_management"
        _apply_headline_escalation_setup_impact(out, status)
        return out
    if status not in WAIT_REPLACEABLE_STATUSES:
        out["publication_type"] = "active_signal_update"
        out["replacement_reason"] = "existing_signal_confirmed_or_armed"
        _apply_headline_escalation_setup_impact(out, status)
        return out
    if old.get("filled"):
        out["replacement_reason"] = "old_signal_already_filled"
        _apply_headline_escalation_setup_impact(out, status)
        return out
    if candidate.get("no_trade") or candidate.get("hard_block_conditions"):
        out["replacement_reason"] = "candidate_has_hard_block"
        _apply_headline_escalation_setup_impact(out, status)
        return out

    rr_not_worse = rr_old is None or rr_new is None or rr_new + 0.0001 >= rr_old
    old_conf = old.get("confidence_rank")
    new_conf = candidate.get("confidence_rank")
    confidence_not_worse = old_conf is None or new_conf is None or new_conf >= old_conf
    old_risk = abs(float(old["entry_price"]) - float(old["sl"])) if old.get("entry_price") is not None and old.get("sl") is not None else None
    new_risk = abs(float(candidate["entry_price"]) - float(candidate["sl"])) if candidate.get("entry_price") is not None and candidate.get("sl") is not None else None
    sl_not_wider = old_risk is None or new_risk is None or new_risk <= old_risk * 1.0025
    ema20 = candidate.get("ema20_m15")
    closer_to_structure = False
    if ema20 is not None and old.get("entry_price") is not None and candidate.get("entry_price") is not None:
        closer_to_structure = abs(float(candidate["entry_price"]) - ema20) < abs(float(old["entry_price"]) - ema20)
    materially_better = closer_to_structure or (rr_old is not None and rr_new is not None and rr_new > rr_old + 0.05) or (
        old_conf is not None and new_conf is not None and new_conf > old_conf
    ) or (old_risk is not None and new_risk is not None and new_risk < old_risk * 0.995)

    if rr_not_worse and confidence_not_worse and sl_not_wider and materially_better:
        out["publication_type"] = "replace_wait_confirm"
        out["replacement_selected"] = True
        out["replacement_reason"] = "updated_wait_confirm_levels_materially_better"
        out["replaced_signal_id"] = old.get("signal_id")
    else:
        out["replacement_reason"] = "replacement_not_materially_better"
    _apply_headline_escalation_setup_impact(out, status)
    return out


def _msk_label_from_signal_id(signal_id: str | None) -> str:
    text = str(signal_id or "")
    try:
        if len(text) >= 13 and text[8] == "_":
            return f"{text[9:11]}:{text[11:13]} МСК"
    except Exception:
        pass
    return "ранее"


def _first_nonempty(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _topic_label(value) -> str:
    if isinstance(value, dict):
        return str(value.get("topic_id") or value.get("topic") or value.get("name") or "").strip()
    if isinstance(value, list):
        for item in value:
            label = _topic_label(item)
            if label:
                return label
        return ""
    return str(value or "").strip()


def _event_risk_field(decision: dict, candidate: dict, key: str):
    event_risk = candidate.get("event_risk") if isinstance(candidate.get("event_risk"), dict) else {}
    return _first_nonempty(decision.get(key), candidate.get(key), event_risk.get(key))


def _apply_headline_escalation_setup_impact(out: dict, status: str) -> None:
    if str(out.get("headline_risk_delta") or "").strip().upper() != "ESCALATION":
        return
    out["headline_update_generated"] = True
    out["duplicate_decision"] = "headline_risk_update"
    if status == "WAIT_CONFIRM":
        out["confirmation_required"] = True
        out["confirmation_required_reason"] = "headline_risk_escalation"
        out["management_review_required"] = True
        out["management_review_reason"] = "headline_risk_escalation"
    elif status in {"CONFIRM_LIVE", "SETUP_ARMED"}:
        out["management_review_required"] = True
        out["management_review_reason"] = "headline_risk_escalation"
    elif status in LIVE_POSITION_STATUSES:
        out["risk_reassessment_required"] = True
        out["risk_reassessment_reason"] = "headline_risk_escalation"


def _headline_risk_advisory(decision: dict, candidate: dict) -> str:
    old = decision.get("duplicate_signal") if isinstance(decision.get("duplicate_signal"), dict) else {}
    status = str(old.get("status") or decision.get("duplicate_signal_status") or "").upper()
    publication_type = str(decision.get("publication_type") or "").strip().lower()
    if publication_type != "management_update" and status not in LIVE_POSITION_STATUSES:
        return ""

    level = str(_event_risk_field(decision, candidate, "event_risk_level") or "").strip().lower()
    delta = str(_event_risk_field(decision, candidate, "headline_risk_delta") or "NONE").strip().upper()
    if level not in {"high", "severe", "critical"} and delta != "ESCALATION":
        return ""

    bias = str(_event_risk_field(decision, candidate, "event_bias") or "").strip().lower()
    topic = _topic_label(_event_risk_field(decision, candidate, "dominant_critical_topic"))
    if not topic:
        topic = _topic_label(_event_risk_field(decision, candidate, "critical_topics"))
    side = str(candidate.get("direction") or old.get("direction") or "").strip().lower()
    conflicts_active_side = direction_conflicts_event_bias(side, bias)
    if not topic and not conflicts_active_side:
        return ""

    lines = [
        "",
        "",
        "⚠️ HEADLINE RISK UPDATE",
        "",
        "Duplicate full signal remains suppressed, but fresh context shows elevated headline pressure.",
    ]
    previous_level = _event_risk_field(decision, candidate, "previous_risk_level")
    new_level = _event_risk_field(decision, candidate, "new_risk_level") or level
    reason = _event_risk_field(decision, candidate, "risk_transition_reason")
    if previous_level:
        lines.append(f"- previous_risk_level: {previous_level}")
    if new_level:
        lines.append(f"- new_risk_level: {new_level}")
    if delta:
        lines.append(f"- headline_risk_delta: {delta}")
    if reason:
        lines.append(f"- risk_transition_reason: {reason}")
    if level:
        lines.append(f"- event_risk_level: {level}")
    if bias:
        lines.append(f"- event_bias: {bias}")
    if topic:
        lines.append(f"- topic: {topic}")
    if conflicts_active_side:
        lines.append("- active side exposure: current direction conflicts with headline-risk bias")
    lines.extend(
        [
            "",
            "Continuation quality is downgraded while this risk is active.",
            "Do not add exposure automatically; fresh management re-check is required before treating this as normal continuation.",
        ]
    )
    return "\n".join(lines)


def _state_guard_update_lineage(state_guard: dict, candidate: dict, publication_type: str) -> dict:
    previous_signal_id = state_guard.get("state_guard_primary_signal_id")
    previous_state = str(state_guard.get("state_guard_primary_lifecycle_state") or "").strip().upper() or None
    previous_entry = _try_float(state_guard.get("state_guard_primary_entry"))
    new_entry = _try_float(candidate.get("entry_price"))
    entry_distance = _distance_pct(previous_entry, new_entry)
    pending_states = {"WAIT_CONFIRM", "WAIT_POST_EVENT_REPRICE", "CONFIRM_LIVE", "SETUP_ARMED"}
    is_pending = previous_state in pending_states
    material_reprice = entry_distance is not None and entry_distance >= 0.5
    classification = "management_update" if publication_type == "management_update" else "active_signal_refresh"
    replacement_reason = str(state_guard.get("state_guard_reason") or "active_scenario_exists")
    if is_pending and material_reprice:
        classification = "reprice_review_pending_setup"
        replacement_reason = "pending_setup_reprice_review_required"
    elif is_pending:
        classification = "refresh_pending_setup"

    return {
        "related_signal_id": previous_signal_id,
        "refresh_of_signal_id": previous_signal_id if publication_type == "active_signal_update" else None,
        "replaces_signal_id": None,
        "previous_entry": previous_entry,
        "new_entry": new_entry,
        "previous_lifecycle_state": previous_state,
        "replacement_reason": replacement_reason,
        "pending_update_classification": classification,
        "entry_distance_pct": round(entry_distance, 6) if entry_distance is not None else None,
    }


def _recommended_non_full_publication_type(state_guard: dict) -> str:
    decision = str(state_guard.get("state_guard_decision") or "").strip().upper()
    recommended = str(state_guard.get("state_guard_recommended_publication_type") or "").strip().upper()
    primary_state = str(state_guard.get("state_guard_primary_lifecycle_state") or "").strip().upper()
    previous_runner_status = str(state_guard.get("state_guard_previous_runner_status") or "").strip().lower()
    reason = str(state_guard.get("state_guard_reason") or "").strip().lower()
    allowed = {
        "MANAGEMENT_UPDATE",
        "ACTIVE_SIGNAL_UPDATE",
        "RUNNER_MANAGEMENT_UPDATE",
        "REPRICE_UPDATE",
        "CANCEL_AND_REPLACE",
        "RE_ENTRY_SIGNAL",
        "ADD_ON_REVIEW",
        "SUPPRESS_DUPLICATE",
    }
    for candidate in (recommended, decision):
        if candidate in allowed:
            return candidate
    if decision == "SUPPRESS_DUPLICATE":
        return "SUPPRESS_DUPLICATE"
    if primary_state in LIVE_POSITION_STATUSES:
        if "RUNNER" in primary_state or previous_runner_status == "active":
            return "RUNNER_MANAGEMENT_UPDATE"
        return "MANAGEMENT_UPDATE"
    if primary_state in {"WAIT_CONFIRM", "WAIT_POST_EVENT_REPRICE", "CONFIRM_LIVE", "SETUP_ARMED"}:
        if "reprice" in reason:
            return "REPRICE_UPDATE"
        return "ACTIVE_SIGNAL_UPDATE"
    if "reprice" in reason:
        return "REPRICE_UPDATE"
    return "ACTIVE_SIGNAL_UPDATE"


def build_state_guard_enforcement_decision(state_guard: dict, candidate: dict, cfg: SchedulerConfig) -> dict:
    status = str(state_guard.get("state_guard_status") or "").strip().lower()
    can_publish = state_guard.get("state_guard_can_publish_full_signal")
    override_enabled = bool(cfg.scheduled_state_guard_manual_override_enabled)
    override_reason = str(cfg.scheduled_state_guard_manual_override_reason or "").strip()
    override_used = status == "ok" and can_publish is False and override_enabled and bool(override_reason)
    override_source = "config:SCHEDULED_STATE_GUARD_MANUAL_OVERRIDE_*" if override_used else None

    result = {
        "state_guard_manual_override_enabled": override_enabled,
        "state_guard_manual_override_used": override_used,
        "state_guard_manual_override_reason": override_reason or None,
        "state_guard_manual_override_source": override_source,
    }
    if status != "ok" or can_publish is not False:
        result.update(
            {
                "state_guard_runtime_blocked_full_signal": False,
                "scheduled_state_guard_enforcement_action": "none",
                "scheduled_state_guard_duplicate_runtime_action": "none",
            }
        )
        return result

    if override_used:
        result.update(
            {
                "state_guard_runtime_blocked_full_signal": False,
                "scheduled_state_guard_enforcement_action": "manual_override_allow_full_signal",
                "scheduled_state_guard_duplicate_runtime_action": "manual_override_allow_full_signal",
            }
        )
        return result

    publication_type = _recommended_non_full_publication_type(state_guard)
    if publication_type == "RE_ENTRY_SIGNAL" and state_guard.get("state_guard_reentry_allowed") is not True:
        publication_type = "SUPPRESS_DUPLICATE"
    publication_type = publication_type.lower()
    lineage = _state_guard_update_lineage(state_guard, candidate, publication_type)
    headline_delta = _event_risk_field({}, candidate, "headline_risk_delta") or state_guard.get("headline_risk_delta") or "NONE"
    previous_state = str(state_guard.get("state_guard_primary_lifecycle_state") or "").strip().upper()
    result.update(
        {
            "state_guard_runtime_blocked_full_signal": True,
            "scheduled_state_guard_enforcement_action": "enforce_non_full_signal",
            "scheduled_state_guard_duplicate_runtime_action": "enforce_non_full_signal",
            "publication_type": publication_type,
            "duplicate_signal_id": state_guard.get("state_guard_primary_signal_id"),
            "duplicate_signal_status": state_guard.get("state_guard_primary_lifecycle_state"),
            "replacement_reason": lineage.get("replacement_reason")
            or state_guard.get("state_guard_reason")
            or "state_guard_blocked_full_signal",
            "duplicate_signal": {
                "signal_id": state_guard.get("state_guard_primary_signal_id"),
                "status": state_guard.get("state_guard_primary_lifecycle_state"),
                "display_symbol": state_guard.get("state_guard_active_same_direction_symbol") or candidate.get("display_symbol"),
                "direction": state_guard.get("state_guard_active_same_direction_direction")
                or state_guard.get("state_guard_primary_direction")
                or candidate.get("direction"),
                "entry_price": state_guard.get("state_guard_primary_entry"),
                "sl": state_guard.get("state_guard_primary_sl"),
            },
            **lineage,
            "headline_risk_delta": headline_delta,
            "previous_risk_level": _event_risk_field({}, candidate, "previous_risk_level") or state_guard.get("previous_risk_level"),
            "new_risk_level": _event_risk_field({}, candidate, "new_risk_level") or state_guard.get("new_risk_level"),
            "risk_transition_reason": _event_risk_field({}, candidate, "risk_transition_reason") or state_guard.get("risk_transition_reason"),
            "duplicate_decision": "headline_risk_update" if str(headline_delta).upper() == "ESCALATION" else "duplicate_headline_same_state",
            "headline_update_generated": str(headline_delta).upper() == "ESCALATION",
            "confirmation_required": str(headline_delta).upper() == "ESCALATION" and previous_state == "WAIT_CONFIRM",
            "confirmation_required_reason": "headline_risk_escalation" if str(headline_delta).upper() == "ESCALATION" and previous_state == "WAIT_CONFIRM" else None,
            "management_review_required": str(headline_delta).upper() == "ESCALATION" and previous_state in {"WAIT_CONFIRM", "CONFIRM_LIVE", "SETUP_ARMED"},
            "management_review_reason": "headline_risk_escalation" if str(headline_delta).upper() == "ESCALATION" and previous_state in {"WAIT_CONFIRM", "CONFIRM_LIVE", "SETUP_ARMED"} else None,
            "risk_reassessment_required": str(headline_delta).upper() == "ESCALATION" and previous_state in LIVE_POSITION_STATUSES,
            "risk_reassessment_reason": "headline_risk_escalation" if str(headline_delta).upper() == "ESCALATION" and previous_state in LIVE_POSITION_STATUSES else None,
        }
    )
    return result


def render_duplicate_update_message(decision: dict, candidate: dict) -> str:
    old = decision.get("duplicate_signal") if isinstance(decision.get("duplicate_signal"), dict) else {}
    symbol = candidate.get("display_symbol") or old.get("display_symbol") or _display_symbol(candidate.get("symbol"))
    side = str(old.get("direction") or candidate.get("direction") or "").upper()
    status = old.get("status") or decision.get("duplicate_signal_status") or "UNKNOWN"
    old_id = old.get("signal_id") or decision.get("duplicate_signal_id")
    if decision.get("publication_type") == "urgent_review":
        strong = bool(decision.get("propose_reverse_signal"))
        if strong:
            return (
                "🚨 URGENT: old setup invalidated before entry\n\n"
                f"{symbol} {str(old.get('direction') or '').upper()} был confirmed/armed, но вход ещё НЕ исполнен.\n\n"
                f"Новый анализ показывает сильный противоположный {side} bias.\n"
                "AIA считает старый setup невалидным.\n\n"
                "Решение:\n"
                "- старый setup поставить на стоп / отменить;\n"
                "- не входить по старому entry;\n"
                f"- рассмотреть новый {side} setup после подтверждения трейдера.\n\n"
                "Это не автоматический reverse-entry."
            )
        return (
            "⚠️ URGENT: opposite bias before entry\n\n"
            f"{symbol} {str(old.get('direction') or '').upper()} уже confirmed/armed, но вход ещё НЕ исполнен.\n\n"
            f"Новый scheduled scan видит противоположный {side} bias.\n\n"
            "Решение AIA:\n"
            "- старый entry поставить на паузу;\n"
            "- не переводить старый setup в ENTRY_LIVE автоматически;\n"
            "- трейдер должен принять решение: отменить старый setup / дождаться reprice / "
            "разрешить старый вход / рассмотреть reverse setup.\n\n"
            "Это не новый автоматический вход."
        )
    if decision.get("publication_type") == "conflict_update":
        final_action = str(
            decision.get("final_management_action")
            or decision.get("management_action")
            or decision.get("action")
            or ""
        ).strip().upper()
        if final_action:
            action_label = str(decision.get("action_label") or "").strip()
            action_reason = str(
                decision.get("action_reason")
                or decision.get("reason_summary")
                or decision.get("short_comment")
                or ""
            ).strip()
            action_confidence = decision.get("action_confidence", decision.get("confidence"))
            next_check = decision.get("next_check", decision.get("next_check_minutes"))
            urgency = str(decision.get("urgency") or "").strip()
            meta_parts = []
            if action_confidence not in (None, ""):
                meta_parts.append(f"confidence={action_confidence}")
            if next_check not in (None, ""):
                meta_parts.append(f"next_check={next_check}m")
            if urgency:
                meta_parts.append(f"urgency={urgency}")
            meta_line = f"\n{' | '.join(meta_parts)}" if meta_parts else ""
            label_line = f" — {action_label}" if action_label else ""
            reason_block = f"\n\nПочему:\n{action_reason}" if action_reason else ""
            return (
                f"🔄 RE_EVAL_ACTIVE_SIGNAL → AIA: {final_action}\n\n"
                f"{symbol} уже в работе, но новый scheduled scan видит противоположный bias.\n\n"
                f"Активный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
                f"Статус AIA: {status}.\n\n"
                "Решение AIA:\n"
                f"{final_action}{label_line}.{reason_block}{meta_line}\n\n"
                "Это не новый вход. Новый противоположный сигнал не публикуем."
            )
        return (
            "🔄 RE_EVAL_ACTIVE_SIGNAL\n\n"
            f"{symbol} уже в работе, но новый scheduled scan видит противоположный bias.\n"
            f"Активный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
            f"Статус AIA: {status}.\n\n"
            "Статус:\n"
            "AIA management decision запрошен.\n"
            "Ждём финальное решение: HOLD / REDUCE / CLOSE_NOW / TRAIL.\n\n"
            "Это не новый вход. Новый противоположный сигнал не публикуем."
        )
    classification = str(decision.get("publication_type") or "").strip().upper()
    if classification == "SUPPRESS_DUPLICATE":
        state_guard_reason = str(
            decision.get("state_guard_reentry_reason")
            or decision.get("state_guard_reason")
            or decision.get("reason")
            or ""
        ).strip()
        if str(status).upper() == "TERMINAL":
            reason_map = {
                "missing_fresh_pullback_or_reset": "fresh pullback/reset ещё не сформирован",
                "cooldown_active": "ещё действует cooldown после завершённого сценария",
                "headline_risk_active": "headline risk сейчас не допускает re-entry",
                "missing_fresh_confirmation": "нет fresh confirmation/reprice для повторного входа",
                "missing_fresh_setup": "нет fresh setup для повторного входа",
                "terminal_ttl_expired_time_sanity_unlock": "time-sanity TTL истёк; stale terminal больше не блокирует сам по себе",
            }
            reason_line = reason_map.get(state_guard_reason, state_guard_reason.replace("_", " ")) if state_guard_reason else ""
            title = (
                "🔄 TERMINAL / COOLDOWN SUPPRESS_DUPLICATE"
                if "cooldown" in state_guard_reason.lower()
                else "🔄 TERMINAL / RE-ENTRY BLOCKED"
            )
            lines = [
                title,
                "",
                f"Предыдущий {symbol} {side} уже завершён.",
                f"Последний связанный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.",
                f"Статус AIA: {status}.",
                "",
                f"Новый scheduled scan снова выбрал {symbol} {side}, но re-entry сейчас не разрешён.",
            ]
            if reason_line:
                lines.append(f"Причина: {reason_line}.")
            elapsed_hours = decision.get("state_guard_reentry_terminal_elapsed_hours")
            ttl_hours = decision.get("state_guard_reentry_terminal_ttl_hours")
            if state_guard_reason == "missing_fresh_pullback_or_reset" and elapsed_hours is not None and ttl_hours is not None:
                lines.append(
                    f"Re-entry заблокирован: fresh reset/pullback не найден, прошло {elapsed_hours:g} из {ttl_hours:g} часов."
                )
            lines.extend(
                [
                    "Новый full_signal не публикуем.",
                    "Для повторного входа нужен fresh setup + reprice/reconfirm.",
                ]
            )
            return "\n".join(lines)
        return (
            "🔄 MANAGEMENT_UPDATE / SUPPRESS_DUPLICATE\n\n"
            f"{symbol} {side} уже имеет активный/связанный сценарий.\n"
            f"Активный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
            f"Статус AIA: {status}.\n\n"
            f"Новый scheduled scan снова выбрал {symbol} {side}.\n"
            "Новый full_signal не публикуем."
        )
    if classification == "RUNNER_MANAGEMENT_UPDATE":
        return (
            "🔄 RUNNER MANAGEMENT UPDATE\n\n"
            f"{symbol} {side} уже сопровождается активным runner.\n"
            f"Активный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
            f"Статус AIA: {status}.\n\n"
            f"Новый scheduled scan снова выбрал {symbol} {side}.\n"
            "Это не новый independent full signal.\n\n"
            "Решение:\n"
            "AIA продолжает сопровождение existing runner / add-on review only."
        ) + _headline_risk_advisory(decision, candidate)
    is_management = classification == "MANAGEMENT_UPDATE" or str(status).upper() in LIVE_POSITION_STATUSES
    pending_update_classification = str(decision.get("pending_update_classification") or "").strip().lower()
    if not is_management and pending_update_classification in {"refresh_pending_setup", "reprice_review_pending_setup"}:
        title = "🔁 ACTIVE SETUP UPDATE"
        previous_entry = decision.get("previous_entry", old.get("entry_price"))
        new_entry = decision.get("new_entry", candidate.get("entry_price"))
        review_line = (
            "Если старый откат уже неактуален, AIA должна явно отменить старый setup и заменить его новым."
            if pending_update_classification == "reprice_review_pending_setup"
            else "Старый setup остаётся основным, а новый расчёт рассматривается как update, не как второй независимый вход."
        )
        return (
            f"{title}\n\n"
            f"{symbol} {side} уже есть в работе как {status}.\n"
            f"Предыдущий сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
            "Entry ещё не исполнен.\n\n"
            f"Новый scheduled scan снова выбрал {symbol} {side}.\n"
            "Это не новый независимый вход.\n\n"
            "Решение:\n"
            "Обновляем/пересматриваем pending setup через AIA.\n"
            f"Старый entry: {previous_entry}\n"
            f"Новый расчётный entry: {new_entry}\n\n"
            f"{review_line}"
        ) + _headline_risk_advisory(decision, candidate)
    title = "🔄 MANAGEMENT_UPDATE" if is_management else "🔄 ACTIVE SIGNAL UPDATE"
    entry_line = "Вход уже активирован." if str(status).upper() in LIVE_POSITION_STATUSES else "Вход ещё не активирован."
    message = (
        f"{title}\n\n"
        f"{symbol} {side} уже в работе.\n"
        f"Активный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
        f"Статус AIA: {status}.\n"
        f"{entry_line}\n\n"
        f"Новый scheduled scan снова выбрал {symbol} {side}, но это та же торговая идея.\n\n"
        "ТВХ:\n"
        f"Старая ТВХ: {old.get('entry_price')}\n"
        f"Новая расчётная ТВХ: {candidate.get('entry_price')}\n\n"
        "Решение:\n"
        "Старый setup остаётся актуальным.\n"
        "Новый сигнал не публикуем, чтобы не дублировать вход.\n"
        "AIA продолжает сопровождать активный сигнал.\n\n"
        + (
            "Это не новый вход. Уже есть активный/managed сценарий; сопровождаем текущую позицию, "
            "не открываем независимый второй full signal."
            if is_management
            else "Это обновление активного сценария, не новый full signal. Старый сигнал остаётся основным, "
            "новые уровни/контекст используются как update."
        )
    )
    return message + _headline_risk_advisory(decision, candidate)


def render_replacement_prefix(decision: dict, candidate: dict) -> str:
    old = decision.get("duplicate_signal") if isinstance(decision.get("duplicate_signal"), dict) else {}
    symbol = candidate.get("display_symbol") or old.get("display_symbol") or _display_symbol(candidate.get("symbol"))
    side = str(candidate.get("direction") or old.get("direction") or "").upper()
    old_id = old.get("signal_id") or decision.get("duplicate_signal_id")
    return (
        "🔁 UPDATED WAIT_CONFIRM / SIGNAL REPLACEMENT\n\n"
        f"{symbol} {side} уже был в ожидании подтверждения.\n"
        f"Старый сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
        "Вход по нему ещё не был исполнен.\n\n"
        "Новый scheduled scan подтвердил тот же сценарий, но уровни стали актуальнее.\n\n"
        f"Старый entry: {old.get('entry_price')}\n"
        f"Новый entry: {candidate.get('entry_price')}\n"
        f"Старый SL: {old.get('sl')}\n"
        f"Новый SL: {candidate.get('sl')}\n\n"
        "Решение:\n"
        "Старый wait_confirm снимаем и заменяем обновлённым setup.\n"
        "AIA дальше подтверждает уже новый уровень.\n\n"
        "Это не второй вход и не scale-in.\n\n"
    )


async def publish_plain_message(cfg: SchedulerConfig, message: str, *, dry_run: bool = False) -> list[int]:
    context = make_context(dry_run=dry_run)
    message_ids: list[int] = []
    for channel in scheduled_full_signal_targets(cfg):
        sent = await context.bot.send_message(chat_id=channel, text=message, parse_mode=None, disable_web_page_preview=True)
        if dry_run:
            message_ids.append(getattr(sent, "message_id", None))
    return [mid for mid in message_ids if mid is not None]


def emit_replacement_event(decision: dict, candidate: dict) -> None:
    append_jsonl(
        SCHEDULED_SIGNAL_LIFECYCLE_EVENTS_PATH,
        {
            "ts": utc_now().isoformat().replace("+00:00", "Z"),
            "status": "REPLACED_BY_NEW_SIGNAL",
            "reason": "replaced_by_updated_wait_confirm",
            "signal_id": decision.get("replaced_signal_id"),
            "replaced_by_signal_id": candidate.get("signal_id"),
            "replacement_reason": decision.get("replacement_reason"),
            "old_entry": decision.get("old_entry"),
            "new_candidate_entry": decision.get("new_candidate_entry"),
            "old_sl": decision.get("old_sl"),
            "new_candidate_sl": decision.get("new_candidate_sl"),
        },
    )


@dataclass
class SchedulerConfig:
    start_date_msk: date
    target_chat_ids: list[int]
    day_enabled: bool
    day_time_msk: str
    mid_enabled: bool
    mid_time_msk: str
    mid_interval_days: int
    signal_enabled: bool
    signal_default_mode: str
    signal_slots_msk: list[str]
    retry_delay_minutes: int
    max_attempts: int
    gate_mode: str
    preferred_mode_downgrade_enabled: bool
    soft_avoid_downgrade: bool
    signal_state_path: Path
    publish_state_path: Path
    due_window_minutes: int
    state_guard_shadow_enabled: bool = True
    state_guard_state_path: Path = DEFAULT_STATE_GUARD_STATE_PATH
    state_guard_max_age_minutes: int = 15
    scheduled_state_guard_duplicate_enforcement_enabled: bool = False
    scheduled_state_guard_manual_override_enabled: bool = False
    scheduled_state_guard_manual_override_reason: str = ""
    dima_scheduled_mode: str = "WINDOW_ONLY"
    dima_chat_id: int = DEFAULT_DIMA_CHAT_ID
    dima_window_cooldown_minutes: int = 90
    dima_macro_update_max_age_minutes: int = 60

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        gate_mode = str(os.getenv("SCHEDULED_SIGNAL_AIA_GATE_MODE", "soft")).strip().lower()
        if gate_mode not in {"strict", "soft", "off"}:
            gate_mode = "soft"
        default_mode = str(os.getenv("SCHEDULED_SIGNAL_DEFAULT_MODE", "aggressive")).strip().lower()
        if default_mode not in {"aggressive", "neutral"}:
            default_mode = "aggressive"
        return cls(
            start_date_msk=parse_date_env("SCHEDULED_START_DATE_MSK", DEFAULT_START_DATE),
            target_chat_ids=parse_chat_ids(),
            day_enabled=parse_bool_env("SCHEDULED_DAY_ENABLED", True),
            day_time_msk=os.getenv("SCHEDULED_DAY_TIME_MSK", "09:00"),
            mid_enabled=parse_bool_env("SCHEDULED_MID_ENABLED", True),
            mid_time_msk=os.getenv("SCHEDULED_MID_TIME_MSK", "08:45"),
            mid_interval_days=max(1, parse_int_env("SCHEDULED_MID_INTERVAL_DAYS", 3)),
            signal_enabled=parse_bool_env("SCHEDULED_SIGNAL_ENABLED", True),
            signal_default_mode=default_mode,
            signal_slots_msk=parse_slots(),
            retry_delay_minutes=max(1, parse_int_env("SCHEDULED_SIGNAL_RETRY_DELAY_MINUTES", 60)),
            max_attempts=max(1, parse_int_env("SCHEDULED_SIGNAL_MAX_ATTEMPTS", 2)),
            gate_mode=gate_mode,
            preferred_mode_downgrade_enabled=parse_bool_env("SCHEDULED_SIGNAL_SOFT_PREFERRED_MODE_DOWNGRADE", False),
            soft_avoid_downgrade=parse_bool_env("SCHEDULED_SIGNAL_SOFT_PREFERRED_MODE_DOWNGRADE", False),
            signal_state_path=PROJECT_ROOT / os.getenv("SCHEDULED_SIGNAL_STATE_PATH", "logs/scheduled_signal_state.json"),
            publish_state_path=PROJECT_ROOT / os.getenv("SCHEDULED_PUBLISH_STATE_PATH", "logs/scheduled_publish_state.json"),
            due_window_minutes=max(1, parse_int_env("SCHEDULED_DUE_WINDOW_MINUTES", 5)),
            state_guard_shadow_enabled=parse_bool_env("STATE_GUARD_SHADOW_ENABLED", True),
            state_guard_state_path=Path(os.getenv("STATE_GUARD_STATE_PATH", str(DEFAULT_STATE_GUARD_STATE_PATH))),
            state_guard_max_age_minutes=max(1, parse_int_env("STATE_GUARD_MAX_AGE_MINUTES", 15)),
            scheduled_state_guard_duplicate_enforcement_enabled=parse_bool_env(
                "SCHEDULED_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED",
                False,
            ),
            scheduled_state_guard_manual_override_enabled=parse_bool_env(
                "SCHEDULED_STATE_GUARD_MANUAL_OVERRIDE_ENABLED",
                False,
            ),
            scheduled_state_guard_manual_override_reason=str(
                os.getenv("SCHEDULED_STATE_GUARD_MANUAL_OVERRIDE_REASON", "")
            ).strip(),
            dima_scheduled_mode=str(os.getenv("TG_DIMA_SCHEDULED_MODE", "WINDOW_ONLY")).strip().upper(),
            dima_chat_id=parse_int_env("TG_DIMA_CHAT_ID", DEFAULT_DIMA_CHAT_ID),
            dima_window_cooldown_minutes=max(1, parse_int_env("DIMA_WINDOW_COOLDOWN_MINUTES", 90)),
            dima_macro_update_max_age_minutes=max(1, parse_int_env("DIMA_MACRO_UPDATE_MAX_AGE_MINUTES", 60)),
        )


def dima_window_only(cfg: SchedulerConfig) -> bool:
    return str(getattr(cfg, "dima_scheduled_mode", "FULL_SIGNAL") or "FULL_SIGNAL").strip().upper() == "WINDOW_ONLY"


def scheduled_full_signal_targets(cfg: SchedulerConfig) -> list[int]:
    profiles = end_user_profiles()
    end_user_by_chat = {p.chat_id: p for p in profiles if p.chat_id is not None}
    dima_chat_id = int(getattr(cfg, "dima_chat_id", DEFAULT_DIMA_CHAT_ID))
    targets = [
        chat_id
        for chat_id in cfg.target_chat_ids
        if chat_id != dima_chat_id
        and (end_user_by_chat.get(chat_id, None) is None or end_user_by_chat[chat_id].receive_scheduled_signals)
    ]
    for profile in profiles:
        if profile.chat_id is not None and profile.receive_scheduled_signals and profile.chat_id not in targets:
            targets.append(profile.chat_id)
    return targets


def publish_decision_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_publish_decisions_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def signal_decision_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_signal_decisions_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def signal_health_audit_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_signal_health_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.json"


def build_daily_signal_health(rows: list[dict]) -> dict:
    rows = [row for row in rows if isinstance(row, dict)]
    funnels = [row.get("candidate_funnel") for row in rows if isinstance(row.get("candidate_funnel"), dict)]
    generated = [funnel for funnel in funnels if funnel.get("candidate_generated")]
    stages = [str(funnel.get("stage") or "") for funnel in funnels]
    contract_valid_stages = {
        FUNNEL_STAGE_POLICY_BLOCKED,
        FUNNEL_STAGE_FULL_SIGNAL_ELIGIBLE,
        FUNNEL_STAGE_WAIT_CONFIRM_PERSISTED,
        FUNNEL_STAGE_PUBLICATION_SENT,
    }
    reason_counts: Counter[str] = Counter()
    distances: list[int] = []
    for funnel in funnels:
        reasons = _clean_list(funnel.get("full_signal_block_reasons"))
        reason_counts.update(str(reason) for reason in reasons if str(reason).strip())
        conditions = _clean_list(funnel.get("conditions_to_eligibility"))
        if funnel.get("candidate_generated"):
            distances.append(len(set(str(item) for item in conditions)))
    published_count = sum(
        1
        for row in rows
        if bool(row.get("published"))
        or str(row.get("decision") or "").lower() in {"publish", "published", "signal_published", "tactical_confirm_signal"}
    )
    contract_valid_count = sum(1 for stage in stages if stage in contract_valid_stages)
    post_generation_block_count = sum(1 for stage in stages if stage == FUNNEL_STAGE_POLICY_BLOCKED)
    if published_count:
        classification = "SIGNAL_DAY"
    elif contract_valid_count and post_generation_block_count:
        classification = "POSSIBLE_OVERFILTER"
    elif generated:
        classification = "SELECTIVE_NO_TRADE"
    else:
        classification = "HEALTHY_NO_TRADE"
    unique_slots = {str(row.get("slot_id")) for row in rows if row.get("slot_id")}
    return {
        "scheduled_slots_count": len(unique_slots),
        "retries_executed_count": sum(1 for row in rows if int(row.get("attempt") or 1) > 1),
        "raw_candidates_count": sum(int(funnel.get("candidate_count") or 0) for funnel in funnels),
        "contract_valid_count": contract_valid_count,
        "wait_confirm_count": sum(1 for funnel in generated if str(funnel.get("entry_mode") or "").lower() == "wait_confirm"),
        "tactical_confirm_count": sum(1 for funnel in funnels if funnel.get("execution_mode") == "TACTICAL_CONFIRM_ONLY"),
        "publication_eligible_count": sum(1 for funnel in funnels if funnel.get("full_signal_eligible") is True),
        "published_count": published_count,
        "pre_generation_block_count": sum(
            1 for funnel in funnels if not funnel.get("candidate_generated") and funnel.get("pre_generation_gate") not in {None, "allowed"}
        ),
        "post_generation_block_count": post_generation_block_count,
        "top_rejection_reasons": [
            {"reason": reason, "count": count}
            for reason, count in reason_counts.most_common(10)
        ],
        "median_candidate_distance_to_eligibility": median(distances) if distances else None,
        # These replay-only metrics require future candles and are deliberately
        # not guessed by the live scheduler.
        "candidates_confirmed_between_slots": None,
        "opportunities_missed_by_schedule": None,
        "unreachable_policy_paths": [],
        "no_signal_day_classification": classification,
    }


def append_signal_decision_with_health(now_utc: datetime, row: dict) -> None:
    decision_path = signal_decision_log_path(now_utc)
    append_jsonl(decision_path, row)
    rows: list[dict] = []
    try:
        for line in decision_path.read_text(encoding="utf-8").splitlines():
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(parsed)
    except (OSError, json.JSONDecodeError):
        return
    audit = build_daily_signal_health(rows)
    audit["audit_only"] = True
    audit["updated_at_utc"] = now_utc.isoformat().replace("+00:00", "Z")
    write_json_atomic(signal_health_audit_path(now_utc), audit)


def scheduled_state_guard_shadow_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_state_guard_shadow_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def _state_guard_base(enabled: bool, status: str) -> dict:
    return {
        "state_guard_shadow_enabled": enabled,
        "state_guard_status": status,
        "state_guard_decision": None,
        "state_guard_decision_reason": None,
        "state_guard_can_publish_full_signal": None,
        "state_guard_recommended_publication_type": None,
        "state_guard_reason": None,
        "state_guard_primary_signal_id": None,
        "state_guard_primary_lifecycle_state": None,
        "state_guard_primary_position_status": None,
        "state_guard_primary_symbol": None,
        "state_guard_primary_direction": None,
        "state_guard_active_same_direction_symbol": None,
        "state_guard_active_same_direction_direction": None,
        "state_guard_active_same_direction_lifecycle": None,
        "state_guard_duplicate_detected": None,
        "state_guard_conflict_detected": None,
        "state_guard_replacement_candidate": None,
        "state_guard_entry_distance_pct": None,
        "state_guard_sl_distance_pct": None,
        "state_guard_secondary_signal_ids": [],
        "state_guard_explanation": None,
        "state_guard_previous_signal_id": None,
        "state_guard_previous_outcome": None,
        "state_guard_previous_tp_reached": None,
        "state_guard_previous_runner_status": None,
        "state_guard_reentry_signal": None,
        "state_guard_reentry_allowed": None,
        "state_guard_reentry_reason": None,
        "state_guard_entry_blocked": None,
        "state_guard_entry_blocked_reason": None,
        "state_guard_old_signal_id": None,
        "state_guard_old_direction": None,
        "state_guard_new_bias_direction": None,
        "state_guard_previous_direction": None,
        "state_guard_new_direction": None,
        "state_guard_direction_match": None,
        "state_guard_regime_flip_candidate": None,
        "state_guard_regime_flip_reason": None,
        "state_guard_human_decision_required": None,
        "state_guard_old_setup_recommendation": None,
        "state_guard_propose_reverse_signal": None,
    }


def build_state_guard_candidate(candidate: dict, *, source: str = "scheduled", created_at: str | None = None) -> dict:
    return {
        "signal_id": candidate.get("signal_id"),
        "symbol": candidate.get("display_symbol") or candidate.get("symbol"),
        "direction": candidate.get("direction"),
        "entry": _try_float(candidate.get("entry") if candidate.get("entry") is not None else candidate.get("entry_price")),
        "sl": _try_float(candidate.get("sl")),
        "tp1": _try_float(candidate.get("tp1")),
        "tp2": _try_float(candidate.get("tp2")),
        "tp3": _try_float(candidate.get("tp3")),
        "mode": candidate.get("mode"),
        "rr": _rr_for_signal(candidate),
        "source": source,
        "created_at": created_at,
        "strategy_type": candidate.get("strategy_type"),
        "confidence": candidate.get("confidence"),
        "score": candidate.get("score") or candidate.get("confidence_score"),
    }


def _load_state_guard_api():
    repo_root = str(AGENT_STATE_REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from app.state.state_query import evaluate_candidate_signal

    return evaluate_candidate_signal


def evaluate_state_guard_shadow(candidate: dict, cfg: SchedulerConfig, *, now_utc: datetime | None = None) -> dict:
    if not cfg.state_guard_shadow_enabled:
        return _state_guard_base(False, "disabled")
    now_utc = now_utc or utc_now()
    state_path = cfg.state_guard_state_path
    out = _state_guard_base(True, "unavailable")
    try:
        stat = state_path.stat()
    except FileNotFoundError:
        return out
    except Exception as exc:
        out["state_guard_explanation"] = f"state file unavailable: {exc}"
        return out

    age_seconds = max(0.0, now_utc.timestamp() - stat.st_mtime)
    if age_seconds > cfg.state_guard_max_age_minutes * 60:
        out["state_guard_status"] = "stale"
        out["state_guard_explanation"] = (
            f"state file older than {cfg.state_guard_max_age_minutes} minutes "
            f"({round(age_seconds / 60.0, 2)} minutes)"
        )
        return out

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        out["state_guard_explanation"] = f"state file unavailable: {exc}"
        return out
    if not isinstance(state, dict):
        out["state_guard_explanation"] = "state file did not contain a JSON object"
        return out

    guard_candidate = build_state_guard_candidate(
        candidate,
        created_at=now_utc.isoformat().replace("+00:00", "Z"),
    )
    try:
        evaluate_candidate_signal = _load_state_guard_api()
        evaluation = evaluate_candidate_signal(guard_candidate, state)
    except Exception as exc:
        LOGGER.exception("State Query Guard shadow evaluation failed")
        out["state_guard_status"] = "error"
        out["state_guard_explanation"] = str(exc)
        return out

    out.update(
        {
            "state_guard_status": "ok",
            "state_guard_decision": evaluation.get("decision"),
            "state_guard_decision_reason": evaluation.get("decision_reason") or evaluation.get("reason"),
            "state_guard_can_publish_full_signal": evaluation.get("can_publish_full_signal"),
            "state_guard_recommended_publication_type": evaluation.get("recommended_publication_type"),
            "state_guard_reason": evaluation.get("reason"),
            "state_guard_primary_signal_id": evaluation.get("primary_signal_id"),
            "state_guard_primary_lifecycle_state": evaluation.get("primary_lifecycle_state"),
            "state_guard_primary_position_status": evaluation.get("primary_position_status"),
            "state_guard_primary_symbol": evaluation.get("active_symbol") or evaluation.get("candidate_symbol"),
            "state_guard_primary_direction": evaluation.get("active_direction"),
            "state_guard_active_same_direction_symbol": evaluation.get("active_symbol"),
            "state_guard_active_same_direction_direction": evaluation.get("active_direction"),
            "state_guard_active_same_direction_lifecycle": evaluation.get("active_lifecycle"),
            "state_guard_active_same_direction_scenario_found": evaluation.get("active_same_direction_scenario_found"),
            "state_guard_active_same_direction_signal_id": evaluation.get("active_same_direction_signal_id")
            or evaluation.get("primary_signal_id"),
            "state_guard_active_same_direction_status": evaluation.get("active_same_direction_status")
            or evaluation.get("primary_lifecycle_state"),
            "state_guard_duplicate_detected": evaluation.get("duplicate_detected"),
            "state_guard_conflict_detected": evaluation.get("conflict_detected"),
            "state_guard_replacement_candidate": evaluation.get("replacement_candidate"),
            "state_guard_entry_distance_pct": evaluation.get("entry_distance_pct"),
            "state_guard_sl_distance_pct": evaluation.get("sl_distance_pct"),
            "state_guard_secondary_signal_ids": evaluation.get("secondary_signal_ids") or [],
            "state_guard_explanation": evaluation.get("explanation"),
            "state_guard_previous_signal_id": evaluation.get("previous_signal_id"),
            "state_guard_previous_outcome": evaluation.get("previous_outcome"),
            "state_guard_previous_tp_reached": evaluation.get("previous_tp_reached"),
            "state_guard_previous_runner_status": evaluation.get("previous_runner_status"),
            "state_guard_reentry_signal": evaluation.get("reentry_signal"),
            "state_guard_reentry_allowed": evaluation.get("reentry_allowed"),
            "state_guard_reentry_reason": evaluation.get("reentry_reason"),
            "state_guard_reentry_terminal_ttl_expired": evaluation.get("reentry_terminal_ttl_expired"),
            "state_guard_reentry_allowed_by_time_sanity": evaluation.get("reentry_allowed_by_time_sanity"),
            "state_guard_reentry_terminal_elapsed_hours": evaluation.get("reentry_terminal_elapsed_hours"),
            "state_guard_reentry_terminal_ttl_hours": evaluation.get("reentry_terminal_ttl_hours"),
            "state_guard_entry_blocked": evaluation.get("entry_blocked"),
            "state_guard_entry_blocked_reason": evaluation.get("entry_blocked_reason"),
            "state_guard_old_signal_id": evaluation.get("old_signal_id"),
            "state_guard_old_direction": evaluation.get("old_direction"),
            "state_guard_new_bias_direction": evaluation.get("new_bias_direction"),
            "state_guard_previous_direction": evaluation.get("previous_direction"),
            "state_guard_new_direction": evaluation.get("new_direction"),
            "state_guard_direction_match": evaluation.get("direction_match"),
            "state_guard_regime_flip_candidate": evaluation.get("regime_flip_candidate"),
            "state_guard_regime_flip_reason": evaluation.get("regime_flip_reason"),
            "state_guard_human_decision_required": evaluation.get("human_decision_required"),
            "state_guard_old_setup_recommendation": evaluation.get("old_setup_recommendation"),
            "state_guard_propose_reverse_signal": evaluation.get("propose_reverse_signal"),
            "active_same_direction_scenario_found": evaluation.get("active_same_direction_scenario_found"),
            "active_same_direction_signal_id": evaluation.get("active_same_direction_signal_id")
            or evaluation.get("primary_signal_id"),
            "active_same_direction_status": evaluation.get("active_same_direction_status")
            or evaluation.get("primary_lifecycle_state"),
            "active_same_direction_symbol": evaluation.get("active_symbol"),
            "active_same_direction_direction": evaluation.get("active_direction"),
            "active_same_direction_lifecycle": evaluation.get("active_lifecycle"),
            "duplicate_detected": evaluation.get("duplicate_detected"),
        }
    )
    primary_signal_id = out.get("state_guard_primary_signal_id")
    if primary_signal_id:
        primary_trade = ((state.get("trades") or {}).get(str(primary_signal_id)) or {})
        snapshot = primary_trade.get("trade_plan_snapshot") or primary_trade.get("signal_snapshot") or {}
        out.update(
            {
                "state_guard_primary_entry": _try_float(snapshot.get("entry")),
                "state_guard_primary_sl": _try_float(snapshot.get("sl")),
                "state_guard_primary_tp1": _try_float(snapshot.get("tp1")),
                "state_guard_primary_tp2": _try_float(snapshot.get("tp2")),
                "state_guard_primary_tp3": _try_float(snapshot.get("tp3")),
            }
        )
    return out


def state_guard_duplicate_runtime_action(state_guard: dict, cfg: SchedulerConfig) -> dict:
    duplicate_decision = str(state_guard.get("state_guard_decision") or "").strip().upper()
    recommended = str(state_guard.get("state_guard_recommended_publication_type") or "").strip().upper()
    can_publish = state_guard.get("state_guard_can_publish_full_signal")
    active_status = str(
        state_guard.get("state_guard_active_same_direction_status")
        or state_guard.get("active_same_direction_status")
        or state_guard.get("state_guard_primary_lifecycle_state")
        or ""
    ).strip().upper()
    same_direction_active = bool(
        state_guard.get("state_guard_active_same_direction_scenario_found")
        or state_guard.get("active_same_direction_scenario_found")
    )
    eligible = (
        same_direction_active
        and can_publish is False
        and active_status in ACTIVE_SAME_DIRECTION_DUPLICATE_BLOCKING_STATUSES
        and (
            duplicate_decision in {"ACTIVE_SIGNAL_UPDATE", "MANAGEMENT_UPDATE", "SUPPRESS_DUPLICATE"}
            or recommended in {"ACTIVE_SIGNAL_UPDATE", "MANAGEMENT_UPDATE", "SUPPRESS_DUPLICATE"}
        )
    )
    action = (
        "enforce_duplicate_suppression"
        if eligible and cfg.scheduled_state_guard_duplicate_enforcement_enabled
        else "shadow_only"
        if eligible
        else "none"
    )
    enabled = bool(cfg.scheduled_state_guard_duplicate_enforcement_enabled)
    return {
        "scheduled_state_guard_duplicate_enforcement_enabled": enabled,
        "scheduled_state_guard_duplicate_runtime_eligible": eligible,
        "scheduled_state_guard_duplicate_runtime_action": action,
        "scheduled_state_guard_enforcement_action": action,
        "duplicate_enforcement_enabled": enabled,
        "duplicate_enforcement_action": action,
    }


def _day_bias_to_direction(value) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    if text in {"short", "bearish", "risk_off", "down", "sell"}:
        return "short"
    if text in {"long", "bullish", "risk_on", "up", "buy"}:
        return "long"
    return None


def day_bias_diagnostics(payload: dict, candidate: dict, gate: dict) -> dict:
    ctx = payload.get("day_mid_context") if isinstance(payload.get("day_mid_context"), dict) else {}
    raw_day_bias = (
        ctx.get("day_bias")
        or payload.get("day_bias")
        or payload.get("day_bias_direction")
        or gate.get("day_bias")
        or gate.get("event_bias")
    )
    day_bias_direction = _day_bias_to_direction(raw_day_bias)
    candidate_direction = _normalize_direction(candidate.get("direction"))
    vs = "unknown"
    counter = False
    if day_bias_direction and candidate_direction:
        vs = "aligned" if day_bias_direction == candidate_direction else "counter"
        counter = vs == "counter"
    market_override = bool(payload.get("market_override_detected") or payload.get("market_override") or payload.get("override_detected"))
    regime_flip_reason = (
        payload.get("regime_flip_reason")
        or payload.get("counter_regime_allowed_reason")
        or gate.get("regime_flip_reason")
        or gate.get("counter_regime_allowed_reason")
    )
    allowed_reason = None
    if counter:
        allowed_reason = regime_flip_reason or payload.get("market_override_reason") or ("market_override_detected" if market_override else "none")
    return {
        "day_bias_direction": day_bias_direction or "unknown",
        "candidate_direction": candidate_direction or "unknown",
        "signal_direction_vs_day_bias": vs,
        "counter_regime_signal": counter,
        "counter_regime_allowed_reason": allowed_reason,
        "regime_flip_candidate": counter,
        "regime_flip_reason": regime_flip_reason,
        "market_override_detected": market_override,
    }


def state_guard_shadow_log_row(
    now_utc: datetime,
    slot_id: str,
    result: dict,
) -> dict:
    payload = result.get("last_payload") if isinstance(result.get("last_payload"), dict) else {}
    candidate = _candidate_from_payload(payload, signal_id=result.get("signal_id"))
    return {
        "ts": now_utc.isoformat().replace("+00:00", "Z"),
        "slot_id": slot_id,
        "candidate_signal_id": result.get("candidate_signal_id") or result.get("signal_id"),
        "symbol": candidate.get("display_symbol") or candidate.get("symbol"),
        "direction": candidate.get("direction"),
        "active_symbol": result.get("state_guard_primary_symbol") or result.get("active_same_direction_symbol"),
        "active_direction": result.get("state_guard_primary_direction") or result.get("active_same_direction_direction"),
        "active_lifecycle": result.get("state_guard_primary_lifecycle_state") or result.get("active_same_direction_lifecycle"),
        "candidate_symbol": candidate.get("display_symbol") or candidate.get("symbol"),
        "candidate_direction": candidate.get("direction"),
        "decision_reason": result.get("state_guard_decision_reason") or result.get("state_guard_reason"),
        "direction_match": result.get("state_guard_direction_match"),
        "previous_direction": result.get("state_guard_previous_direction"),
        "new_direction": result.get("state_guard_new_direction"),
        "regime_flip_candidate": result.get("state_guard_regime_flip_candidate"),
        "regime_flip_reason": result.get("state_guard_regime_flip_reason"),
        "candidate_entry": candidate.get("entry_price"),
        "candidate_sl": candidate.get("sl"),
        "candidate_rr": _rr_for_signal(candidate),
        "guard_status": result.get("state_guard_status"),
        "guard_decision": result.get("state_guard_decision"),
        "can_publish_full_signal": result.get("state_guard_can_publish_full_signal"),
        "recommended_publication_type": result.get("state_guard_recommended_publication_type"),
        "reason": result.get("state_guard_reason"),
        "primary_signal_id": result.get("state_guard_primary_signal_id"),
        "primary_lifecycle_state": result.get("state_guard_primary_lifecycle_state"),
        "primary_position_status": result.get("state_guard_primary_position_status"),
        "primary_entry": result.get("state_guard_primary_entry"),
        "primary_sl": result.get("state_guard_primary_sl"),
        "primary_tp1": result.get("state_guard_primary_tp1"),
        "primary_tp2": result.get("state_guard_primary_tp2"),
        "primary_tp3": result.get("state_guard_primary_tp3"),
        "duplicate_detected": result.get("state_guard_duplicate_detected"),
        "conflict_detected": result.get("state_guard_conflict_detected"),
        "replacement_candidate": result.get("state_guard_replacement_candidate"),
        "previous_signal_id": result.get("state_guard_previous_signal_id"),
        "previous_outcome": result.get("state_guard_previous_outcome"),
        "previous_tp_reached": result.get("state_guard_previous_tp_reached"),
        "previous_runner_status": result.get("state_guard_previous_runner_status"),
        "reentry_signal": result.get("state_guard_reentry_signal"),
        "reentry_allowed": result.get("state_guard_reentry_allowed"),
        "reentry_reason": result.get("state_guard_reentry_reason"),
        "reentry_terminal_ttl_expired": result.get("state_guard_reentry_terminal_ttl_expired"),
        "reentry_allowed_by_time_sanity": result.get("state_guard_reentry_allowed_by_time_sanity"),
        "reentry_terminal_elapsed_hours": result.get("state_guard_reentry_terminal_elapsed_hours"),
        "reentry_terminal_ttl_hours": result.get("state_guard_reentry_terminal_ttl_hours"),
        "entry_blocked": result.get("state_guard_entry_blocked"),
        "entry_blocked_reason": result.get("state_guard_entry_blocked_reason"),
        "old_signal_id": result.get("state_guard_old_signal_id"),
        "old_direction": result.get("state_guard_old_direction"),
        "new_bias_direction": result.get("state_guard_new_bias_direction"),
        "human_decision_required": result.get("state_guard_human_decision_required"),
        "old_setup_recommendation": result.get("state_guard_old_setup_recommendation"),
        "propose_reverse_signal": result.get("state_guard_propose_reverse_signal"),
        "explanation": result.get("state_guard_explanation"),
    }


def is_due(now_msk: datetime, scheduled_msk: datetime, window_minutes: int) -> bool:
    return scheduled_msk <= now_msk < scheduled_msk + timedelta(minutes=window_minutes)


def day_due(now_utc: datetime, cfg: SchedulerConfig) -> tuple[bool, datetime]:
    now_msk = to_msk(now_utc)
    scheduled = slot_datetime_msk(now_msk.date(), cfg.day_time_msk)
    return now_msk.date() >= cfg.start_date_msk and is_due(now_msk, scheduled, cfg.due_window_minutes), scheduled


def mid_due(now_utc: datetime, cfg: SchedulerConfig) -> tuple[bool, datetime, str]:
    now_msk = to_msk(now_utc)
    scheduled = slot_datetime_msk(now_msk.date(), cfg.mid_time_msk)
    if now_msk.date() < cfg.start_date_msk:
        return False, scheduled, ""
    delta = (now_msk.date() - cfg.start_date_msk).days
    due_cycle = delta >= 0 and delta % cfg.mid_interval_days == 0
    cycle_id = mid_cycle_id(now_msk.date(), cfg.start_date_msk, cfg.mid_interval_days) if due_cycle else ""
    return due_cycle and is_due(now_msk, scheduled, cfg.due_window_minutes), scheduled, cycle_id


def due_signal_slots(now_utc: datetime, cfg: SchedulerConfig, state: dict) -> list[tuple[str, datetime, int]]:
    now_msk = to_msk(now_utc)
    out: list[tuple[str, datetime, int]] = []
    if now_msk.date() < cfg.start_date_msk:
        return out
    slots_state = state.setdefault("slots", {})

    for hhmm in cfg.signal_slots_msk:
        scheduled = slot_datetime_msk(now_msk.date(), hhmm)
        sid = slot_id_for(scheduled)
        sstate = slots_state.get(sid)
        if isinstance(sstate, dict) and sstate.get("status") in {"published", "tactical_confirm_signal", "macro_substitution", "cancelled", "deferred", "error"}:
            continue
        if is_due(now_msk, scheduled, cfg.due_window_minutes):
            out.append((sid, scheduled, 1))

    for sid, sstate in list(slots_state.items()):
        if not isinstance(sstate, dict) or sstate.get("status") != "deferred":
            continue
        retry_raw = sstate.get("next_retry_at_msk")
        try:
            retry_at = datetime.fromisoformat(str(retry_raw))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=MSK)
        except Exception:
            continue
        if is_due(now_msk, retry_at.astimezone(MSK), cfg.due_window_minutes):
            try:
                original = datetime.fromisoformat(str(sstate.get("slot_time_msk"))).astimezone(MSK)
            except Exception:
                original = retry_at.astimezone(MSK) - timedelta(minutes=cfg.retry_delay_minutes)
            out.append((sid, original, int(sstate.get("attempt") or 1) + 1))
    return out


def load_aia_context() -> dict:
    paths = [
        Path(os.getenv("AIA_MARKET_WINDOW_PATH", "/root/llm-signal-ai-agent/logs/market_window_advisory_latest.json")),
        Path(os.getenv("AIA_EVENT_RISK_PATH", "/root/llm-signal-ai-agent/logs/event_risk_context_latest.json")),
        Path(os.getenv("AIA_FLOW_CONTEXT_PATH", "/root/llm-signal-ai-agent/logs/flow_derivatives_context_v2.json")),
        PROJECT_ROOT / "logs/market_window_advisory_latest.json",
        PROJECT_ROOT / "logs/event_risk_context_latest.json",
        PROJECT_ROOT / "logs/flow_derivatives_context_v2.json",
    ]
    merged: dict = {}
    missing = True
    for path in paths:
        data = read_json(path)
        if not data:
            continue
        missing = False
        if path.name == "flow_derivatives_context_v2.json":
            merged.setdefault("flow_context", data)
            continue
        for key, value in data.items():
            if key not in merged or merged.get(key) in (None, "", [], {}):
                merged[key] = value
    merged["aia_context_missing"] = missing
    return merged


def load_scheduled_macro_events(now_utc: datetime) -> list[dict]:
    events: list[dict] = []
    for path in (
        PROJECT_ROOT / "logs/last.json",
        PROJECT_ROOT / "logs/macro_event_state.json",
        PROJECT_ROOT / "data/event_calendar.json",
    ):
        data = read_json(path)
        if not data:
            continue
        for key in ("scheduled_macro_events", "calendar_events", "upcoming_events", "events"):
            value = data.get(key)
            if isinstance(value, list):
                events.extend(value)
    return normalize_scheduled_macro_events(
        events,
        source="calendar",
        now=now_utc,
        state_path=PROJECT_ROOT / "logs/macro_event_state.json",
    )


def render_macro_event_message(message_type: str, event: dict, classification: dict | None = None) -> str:
    name = event.get("event_name") or event.get("event") or "Macro event"
    time_msk = event.get("event_time_msk") or event.get("time_msk") or "scheduled time"
    if message_type == "PRE_EVENT_MACRO_NOTICE":
        return (
            f"📊 MACRO EVENT UPDATE • {name} • {time_msk} МСК\n\n"
            "До события действует no-new-entry blackout.\n"
            "Новые входы не открывать; активные позиции только сопровождать."
        )
    if message_type == "MACRO_EVENT_ANALYSIS_PENDING":
        return (
            f"📊 MACRO EVENT UPDATE • {name} • {time_msk} МСК\n\n"
            "Данные вышли, но пост-ивентовая реакция ещё не классифицирована.\n"
            "Новые входы не открывать; ждём 1–2 M15 свечи и оценку BTC/ETH реакции."
        )
    cls = classification if isinstance(classification, dict) else {}
    decision = str(cls.get("execution_policy") or "trade_allowed_strict_confirm").upper()
    allowed = str(cls.get("allowed_direction") or "none").upper()
    actual = cls.get("actual_vs_forecast") or cls.get("core_actual_vs_forecast") or "unknown"
    reaction = cls.get("market_reaction") or "unknown"
    btc = cls.get("btc_reaction") or "unknown"
    old_valid = cls.get("old_narrative_valid")
    if message_type == "MACRO_NO_TRADE_RECOMMENDATION":
        return (
            f"📊 MACRO EVENT UPDATE • {name} • {time_msk} МСК\n\n"
            f"Факт/реакция: {actual}; market read: {reaction}; BTC: {btc}.\n"
            "Execution: реакция хаотичная/неподтверждённая, новые directional entries не открывать.\n\n"
            "Decision: NO_TRADE_CHAOTIC"
        )
    return (
        f"📊 MACRO EVENT UPDATE • {name} • {time_msk} МСК\n\n"
        f"Факт/реакция: {actual}; market read: {reaction}.\n"
        f"BTC reaction: {btc}; old narrative valid: {old_valid}.\n\n"
        f"Execution: allowed direction {allowed}; strict confirm / pullback / retest only, no chase.\n"
        f"Decision: {decision}"
    )


async def publish_macro_event_message(cfg: SchedulerConfig, message: str, *, dry_run: bool = False) -> list[int]:
    context = make_context(dry_run=dry_run)
    message_ids: list[int] = []
    for channel in scheduled_full_signal_targets(cfg):
        sent = await context.bot.send_message(chat_id=channel, text=message, parse_mode=None, disable_web_page_preview=True)
        message_id = getattr(sent, "message_id", None)
        if isinstance(message_id, int):
            message_ids.append(message_id)
    return message_ids


def macro_message_allowed(state: dict, dedupe_key: str, message_type: str, now_utc: datetime, classification: dict | None = None) -> bool:
    dedupe = state.setdefault("macro_dedupe", {})
    existing = dedupe.get(dedupe_key)
    if not isinstance(existing, dict):
        return True
    if message_type == "MACRO_EVENT_UPDATE":
        old_cls = existing.get("post_event_classification")
        return bool(classification and old_cls != classification)
    return False


def _dima_focus(value) -> str:
    text = str(value or "").strip().upper()
    if text in {"", "NONE", "UNKNOWN", "NO FOCUS"}:
        return "none"
    asset = text.split("/", 1)[0]
    return f"{asset}/USDT"


def _dima_direction(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"short", "sell", "bearish"}:
        return "short"
    if text in {"long", "buy", "bullish"}:
        return "long"
    return "observe"


def classify_dima_window(ctx: dict) -> str:
    status = str(ctx.get("aia_status") or ctx.get("status") or "").strip().upper()
    focus = _dima_focus(ctx.get("focus_asset") or ctx.get("focus"))
    direction = _dima_direction(ctx.get("focus_direction") or ctx.get("allowed_direction"))
    event_bias = str(ctx.get("event_bias") or ctx.get("risk_regime") or "").strip().lower()
    event_level = str(ctx.get("event_risk_level") or "").strip().lower()
    confirm_policy = str(ctx.get("confirm_policy") or "").strip().lower()
    execution_mode = str(ctx.get("execution_mode") or "").strip().upper()
    shock = _risk_shock_state(ctx).get("immediate_shock_window")
    if execution_mode == "TACTICAL_CONFIRM_ONLY":
        return "CAUTION_WINDOW"
    if focus != "none" and direction in {"long", "short"} and not shock:
        if direction == "long" and event_bias == "risk_off":
            return "CAUTION_WINDOW"
        if confirm_policy in {"defensive", "block_stale_confirm", "strict"}:
            return "CAUTION_WINDOW"
    if status in {"AVOID", "AVOID_HARD"} or ctx.get("allowed") is False:
        return "AVOID_WINDOW"
    if focus == "none":
        return "AVOID_WINDOW" if event_bias == "risk_off" or event_level == "severe" else "WATCH_ONLY"
    if direction == "long" and event_bias == "risk_off":
        return "CAUTION_WINDOW"
    if confirm_policy in {"defensive", "block_stale_confirm", "strict"}:
        return "CAUTION_WINDOW"
    return "GOOD_WINDOW" if direction in {"long", "short"} else "WATCH_ONLY"


def dima_window_snapshot(ctx: dict) -> dict:
    window_type = classify_dima_window(ctx)
    focus = _dima_focus(ctx.get("focus_asset") or ctx.get("focus"))
    direction = _dima_direction(ctx.get("focus_direction") or ctx.get("allowed_direction"))
    event_bias = str(ctx.get("event_bias") or ctx.get("risk_regime") or "neutral").strip().lower()
    execution_mode = str(ctx.get("execution_mode") or "UNSPECIFIED").strip().upper() or "UNSPECIFIED"
    window_quality = str(ctx.get("window_quality") or window_type).strip().upper() or window_type
    return {
        "window_type": window_type,
        "window_quality": window_quality,
        "execution_mode": execution_mode,
        "focus": focus,
        "direction": direction,
        "risk_regime": event_bias,
        "dedup_key": f"{window_type}|{execution_mode}|{focus}|{direction}|{event_bias}|{window_quality}",
    }


def render_dima_market_window(ctx: dict) -> str:
    snap = dima_window_snapshot(ctx)
    window_type = snap["window_type"]
    focus = "нет" if snap["focus"] == "none" else snap["focus"]
    direction = snap["direction"]
    event_bias = snap["risk_regime"]
    severe = str(ctx.get("event_risk_level") or "").strip().lower() == "severe"
    topic = str(ctx.get("dominant_critical_topic") or "").lower()
    geo_risk = severe or event_bias == "risk_off" or "shipping" in topic or "hormuz" in topic

    if window_type in {"AVOID_WINDOW", "WATCH_ONLY"}:
        situation = (
            "сейчас плохое окно для нового входа."
            if window_type == "AVOID_WINDOW"
            else "рынок пока не даёт ясного торгового окна."
        )
        preferred = "наблюдение"
        mode = "осторожный, без погони за движением"
        actions = [
            "не открывать новые позиции с рынка",
            "не догонять уже начавшееся движение",
            "ждать новый ретест или чистое восстановление уровня с подтверждением",
            "старые идеи перепроверять перед входом",
        ]
        reasons = [
            "активен тяжёлый геополитический риск" if geo_risk else "структура рынка остаётся смешанной",
            "рынок не даёт явного лидера" if snap["focus"] == "none" else "направление пока не подтверждено",
            "фон повышает вероятность ложных выносов",
        ]
        tail_title = "Следующее полезное окно:"
        tail = [
            "шорт — только после ретеста и подтверждённого отбоя вниз",
            "лонг — только после явного восстановления уровня и ослабления давления продавцов",
        ]
    elif direction == "short":
        situation = "рынок слабый; отскоки лучше рассматривать как возможность для продажи."
        preferred = "шорт после подтверждения"
        mode = "ждать ретест, без погони"
        actions = [
            "не продавать внизу после резкого импульса",
            "ждать возврат к средней или зоне ретеста",
            "действовать только после подтверждённого отбоя вниз",
        ]
        reasons = [
            "общий фон остаётся защитным и поддерживает продавцов" if event_bias == "risk_off" else "структура поддерживает продажи от отскока",
            "повышенный событийный риск может ускорить движение вниз" if geo_risk else "продолжение требует подтверждения",
        ]
        tail_title = "Что сломает идею:"
        tail = [
            "закрепление выше средней на часовом графике",
            "ослабление геополитического риска" if geo_risk else "устойчивый разворот структуры вверх",
            "сильный импульс спроса по BTC и ETH",
        ]
    else:
        extended = bool(ctx.get("extended_breakout") or ctx.get("chase_risk") == "high")
        situation = (
            "рынок сохраняет бычью структуру, но вход с текущих уровней запоздал."
            if extended
            else "возможен тактический лонг, но фон остаётся хрупким."
        )
        preferred = "LONG после отката и подтверждения"
        mode = "только WAIT_CONFIRM, без входа с рынка"
        actions = [
            "ждать ретест EMA20 M15 или пробитого уровня",
            "вход только после удержания уровня",
            "не догонять импульс",
            "использовать сниженный риск из-за Ирана/Ормуза" if geo_risk else "использовать сниженный риск до подтверждения",
        ]
        reasons = [
            "DAY/текущая структура допускает LONG-фокус" if direction == "long" else "есть попытка восстановления структуры",
            "фон пока не подходит для агрессивной погони" if geo_risk else "пробой ещё должен подтвердиться",
        ]
        tail_title = "Что отменит сценарий:"
        tail = [
            "потеря локального higher low",
            "возврат под ключевой H1 уровень",
            "новая геополитическая эскалация" if geo_risk else "возврат продавцов",
        ]

    lines = [
        "🧭 Рыночное окно",
        "",
        f"Ситуация: {situation}",
        f"Фокус: {focus}",
        f"Предпочтительное направление: {preferred}",
        f"Режим: {mode}",
        "",
        "Что делать:",
        *[f"- {item}" for item in actions],
        "",
        "Почему:",
        *[f"- {item}" for item in reasons],
        "",
        tail_title,
        *[f"- {item}" for item in tail],
        "",
        "👉 Для точного торгового плана запроси сигнал в канале Dima.",
        "",
        "Это не торговый сигнал.",
    ]
    return "\n".join(lines)


def _dima_window_update_reason(previous: dict | None, current: dict) -> str:
    if not isinstance(previous, dict):
        return "первое сообщение об окне"
    for key, reason in (
        ("risk_regime", "изменился режим риска"),
        ("focus", "изменился рыночный фокус"),
        ("direction", "изменилось предпочтительное направление"),
        ("window_type", "изменилось качество торгового окна"),
    ):
        if previous.get(key) != current.get(key):
            return reason
    return "истёк период защиты от повторов"


def _dima_window_audit_reason(previous: dict | None, current: dict, *, same_slot: bool = False) -> str:
    if same_slot:
        return "same_slot_retry_dedup"
    if not isinstance(previous, dict):
        return "new_scheduled_slot_refresh"
    for key, reason in (
        ("execution_mode", "execution_mode_changed"),
        ("focus", "focus_changed"),
        ("direction", "direction_changed"),
        ("window_quality", "window_quality_changed"),
        ("risk_regime", "material_window_change"),
        ("window_type", "material_window_change"),
    ):
        if previous.get(key) != current.get(key):
            return reason
    return "identical_window_cooldown"


def _dima_context_from_signal_result(gate: dict, result: dict) -> dict:
    ctx = dict(gate)
    if not isinstance(result, dict):
        return ctx

    payload = result.get("last_payload") if isinstance(result.get("last_payload"), dict) else {}
    if payload:
        candidate = _candidate_from_payload(payload, signal_id=result.get("candidate_signal_id") or result.get("signal_id"))
        symbol = candidate.get("display_symbol") or candidate.get("symbol")
        direction = candidate.get("direction")
        if symbol:
            ctx["focus_asset"] = symbol
            ctx.setdefault("focus", symbol)
        if direction:
            ctx["focus_direction"] = direction
            ctx.setdefault("allowed_direction", direction)
        entry_mode = payload.get("entry_mode") or candidate.get("strategy_type")
        if entry_mode:
            ctx["entry_mode"] = entry_mode
        for key in ("extended_breakout", "chase_risk", "explicit_regime_flip_reason"):
            if payload.get(key) not in (None, "", [], {}):
                ctx[key] = payload.get(key)

    for key in (
        "execution_mode",
        "risk_size_mode",
        "entry_mode_required",
        "regime_confirmation_reasons",
        "blocked_reason",
        "candidate_confirm_only",
        "can_publish_full_signal",
        "publication_type",
        "post_generation_event_risk_gate",
        "immediate_shock_window",
        "background_risk_active",
        "immediate_shock_until",
        "material_event_at",
        "last_material_change_at",
        "snapshot_built_at",
    ):
        if result.get(key) not in (None, "", [], {}):
            ctx[key] = result.get(key)
    return ctx


async def maybe_publish_dima_market_window(
    cfg: SchedulerConfig,
    state: dict,
    ctx: dict,
    *,
    now_utc: datetime,
    dry_run: bool = False,
) -> dict:
    snap = dima_window_snapshot(ctx)
    audit = {
        "channel_mode": "MINIMAL_END_USER" if dima_window_only(cfg) else "FULL_SIGNAL",
        "window_message_sent": False,
        "lifecycle_created": False,
        "dima_manual_signal_preserved": True,
        "dima_window_dedup_key": snap["dedup_key"],
        "dima_window_cooldown_applied": False,
        "dima_window_update_reason": None,
        "dima_window_audit_reason": None,
    }
    dima_chat_id = int(getattr(cfg, "dima_chat_id", DEFAULT_DIMA_CHAT_ID))
    if not dima_window_only(cfg) or dima_chat_id not in cfg.target_chat_ids:
        return audit
    # The minimal end-user profile never receives the legacy technical/negative
    # scheduled window. Positive request prompts are owned by AIA presentation.
    audit["dima_window_cooldown_applied"] = True
    audit["dima_window_update_reason"] = "minimal_profile_suppressed"
    audit["dima_window_audit_reason"] = "technical_window_not_allowed"
    return audit


def render_dima_macro_window(event: dict, classification: dict | None = None) -> str:
    raw_name = str(event.get("event_name") or event.get("event") or "событие").strip()
    name = (
        raw_name.replace("Fed Bowman speech", "речь ФРС — Мишель Боуман")
        .replace("Fed speech - ", "речь ФРС — ")
        .replace("Governor Christopher J. Waller", "Кристофер Уоллер")
        .replace("Fed ", "ФРС — ")
    )
    cls = classification if isinstance(classification, dict) else {}
    bias = str(cls.get("bias_after_event") or cls.get("market_bias") or "neutral").lower()
    reaction = "первая реакция BTC и ETH остаётся нейтральной" if bias in {"neutral", "unclear", "unknown"} else "первая реакция рынка уже направленная, но требует подтверждения"
    return "\n".join(
        [
            "📊 Событийное окно",
            "",
            f"Событие: {name}",
            "Статус: реакция ещё не подтверждена",
            "Рекомендация: новые входы не открывать, дождаться одной-двух пятнадцатиминутных свечей",
            "",
            "Что важно:",
            f"- {reaction}",
            "- риск ложного движения повышен",
            "- точный торговый план лучше запрашивать после подтверждения",
            "",
            "Это не торговый сигнал.",
        ]
    )


async def maybe_publish_dima_macro_window(
    cfg: SchedulerConfig,
    event: dict,
    classification: dict | None,
    *,
    now_utc: datetime,
    dry_run: bool = False,
) -> dict:
    audit = {
        "dima_macro_window_sent": False,
        "dima_macro_stale_suppressed": False,
        "dima_macro_profile_suppressed": False,
        "lifecycle_created": False,
    }
    dima_chat_id = int(getattr(cfg, "dima_chat_id", DEFAULT_DIMA_CHAT_ID))
    if not dima_window_only(cfg) or dima_chat_id not in cfg.target_chat_ids:
        return audit
    # Calendar facts and delayed recommendations are rendered by AIA. The
    # legacy scheduled macro window is a technical advisory and is suppressed.
    audit["dima_macro_profile_suppressed"] = True
    return audit


def _norm(value, default="unknown") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _base_asset(value) -> str:
    text = _norm(value, "").upper().replace("-", "/").replace("_", "/")
    if "/" in text:
        return text.split("/", 1)[0]
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)]
    return text


def _candidate_from_context(ctx: dict) -> tuple[str, str]:
    candidate = ctx.get("signal_candidate")
    asset = None
    direction = None
    if isinstance(candidate, dict):
        asset = candidate.get("asset") or candidate.get("symbol")
        direction = candidate.get("direction") or candidate.get("side")
    asset = asset or ctx.get("focus_asset") or "none"
    direction = direction or ctx.get("focus_direction") or "unknown"
    return _base_asset(asset) or "NONE", _norm(direction).upper()


def direction_conflicts_event_bias(direction: str, event_bias: str) -> bool:
    direction = _norm(direction).lower()
    event_bias = _norm(event_bias).lower()
    if event_bias == "risk_off":
        return direction in {"long", "buy", "bullish"}
    if event_bias == "risk_on":
        return direction in {"short", "sell", "bearish"}
    return False


def is_risk_on_alt_long(asset: str, direction: str) -> bool:
    return asset.upper() not in {"BTC", "ETH"} and direction.lower() in {"long", "buy", "bullish"}


def _candidate_entry_mode(candidate: dict) -> str:
    return str(
        candidate.get("entry_mode")
        or candidate.get("strategy_type")
        or candidate.get("entry_type")
        or ""
    ).strip().lower().replace("-", "_")


def _is_confirm_only_candidate(candidate: dict) -> bool:
    mode = _candidate_entry_mode(candidate)
    if mode in {"wait_confirm", "confirm", "confirmation", "retest_confirm"}:
        return True
    status = str(candidate.get("lifecycle_status") or candidate.get("status") or "").strip().upper()
    return status == "WAIT_CONFIRM"


def _risk_shock_state(ctx: dict) -> dict:
    headline_delta = _norm(ctx.get("headline_risk_delta"), "NONE").upper()
    transition = str(ctx.get("risk_transition_reason") or "").strip().lower()
    nearest = ctx.get("nearest_event_minutes")
    try:
        nearest_int = int(nearest) if nearest not in (None, "") else None
    except (TypeError, ValueError):
        nearest_int = None
    explicit_active = ctx.get("immediate_shock_active")
    explicit_window = ctx.get("immediate_shock_window")
    immediate_until = ctx.get("immediate_shock_until")
    until_dt = None
    if immediate_until:
        try:
            until_dt = datetime.fromisoformat(str(immediate_until).replace("Z", "+00:00"))
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=UTC)
        except Exception:
            until_dt = None
    if until_dt is not None:
        immediate = utc_now() < until_dt.astimezone(UTC)
    elif explicit_active is not None:
        immediate = bool(explicit_active)
    elif explicit_window is not None:
        immediate = bool(explicit_window)
    else:
        immediate = bool(
            headline_delta == "ESCALATION"
            or "escalation" in transition
            or (ctx.get("event_risk_window_active") and nearest_int is not None and nearest_int <= 30)
            or (nearest_int is not None and nearest_int <= 30)
        )
    return {
        "immediate_shock_window": immediate,
        "background_risk_active": _norm(ctx.get("event_risk_level"), "").lower() == "severe",
        "immediate_shock_until": immediate_until,
        "material_event_at": ctx.get("material_event_at") or ctx.get("headline_timestamp"),
        "last_material_change_at": ctx.get("last_material_change_at") or ctx.get("headline_timestamp"),
        "snapshot_built_at": ctx.get("snapshot_built_at") or ctx.get("event_risk_generated_at") or ctx.get("generated_at"),
    }


def _counter_risk_tactical_requirements(candidate: dict, ctx: dict) -> dict:
    asset = _base_asset(candidate.get("asset") or candidate.get("symbol") or ctx.get("focus_asset"))
    rules = " ".join(str(item) for item in _clean_list(candidate.get("confirmation_rules"))).lower()
    m15 = _norm(candidate.get("price_vs_ema20_m15") or candidate.get("ema_guard_state"), "").lower()
    h1 = _norm(candidate.get("price_vs_ema20_h1") or candidate.get("ema_guard_state"), "").lower()
    fresh_rule = bool(candidate.get("fresh_reclaim_present")) or (
        any(token in rules for token in ("reclaim", "retest", "hold", "удерж", "возврат", "ретест"))
        and any(token in rules for token in ("m5", "m15"))
    )
    focus = _base_asset(ctx.get("focus_asset"))
    why_asset = str(candidate.get("why_asset") or "").lower()
    allowed_asset = asset in {"BTC", "ETH"} or (asset == "BNB" and bool(candidate.get("bnb_high_quality_eligible")))
    flow_bias = _norm(ctx.get("flow_bias"), "unknown").lower()
    neutral_major_exception = asset in {"BTC", "ETH"} and flow_bias in {"neutral", "unknown", ""} and focus in {"", "NONE"}
    leader_retained = (
        bool(candidate.get("leader_status_retained"))
        or focus == asset
        or "leader" in why_asset
        or "лидер" in why_asset
        or neutral_major_exception
    )
    rr = _rr_for_signal(candidate)
    required_rr = _try_float(candidate.get("required_risk_reward")) or 1.0
    reaction = _norm(candidate.get("market_reaction"), "").lower()
    range_position = _norm(candidate.get("range_position"), "").lower()
    failed: list[str] = []
    checks = (
        ("leader_asset_required", allowed_asset),
        ("wait_confirm_required", _is_confirm_only_candidate(candidate)),
        ("contract_valid_required", not candidate.get("no_trade") and rr is not None and rr >= required_rr),
        ("fresh_m5_m15_reclaim_or_hold_required", fresh_rule),
        ("m15_above_or_reclaimed_ema20_required", m15 in {"above", "above_both", "reclaimed", "bullish"} or bool(candidate.get("fresh_reclaim_present"))),
        ("h1_structure_must_remain_intact", h1 in {"above", "above_both", "reclaimed", "bullish"}),
        ("leader_status_must_be_retained", leader_retained),
        ("volume_confirmation_required_when_available", not candidate.get("volume_confirmation_available") or bool(candidate.get("volume_confirmation")) or "volume" in rules or "объ" in rules),
        ("no_immediate_downside_reaction", reaction not in {"downside", "bearish_break", "breakdown", "risk_flip"}),
        ("not_directly_under_resistance", not bool(candidate.get("directly_under_resistance"))),
        ("not_extended_or_chasing", not bool(candidate.get("overextended_leader_risk")) and _norm(candidate.get("chase_risk"), "").lower() not in {"extended", "active_chase"}),
        ("not_range_middle", range_position not in {"middle", "range_middle", "mid"}),
        ("structure_not_invalidated", not bool(candidate.get("structure_invalidated"))),
    )
    for reason, passed in checks:
        if not passed:
            failed.append(reason)
    return {
        "tactical_counter_risk_allowed": not failed,
        "tactical_counter_risk_failed_reasons": failed,
        "post_headline_reaction_observed": not _risk_shock_state(ctx)["immediate_shock_window"],
        "structure_invalidated": bool(candidate.get("structure_invalidated")),
        "neutral_major_technical_exception": neutral_major_exception,
        "conditions_to_eligibility": failed,
    }


def _regime_confirmation_reasons(candidate: dict, ctx: dict) -> list[str]:
    reasons: list[str] = []
    asset = _base_asset(candidate.get("asset") or candidate.get("symbol") or ctx.get("focus_asset"))
    direction = _normalize_direction(candidate.get("direction") or ctx.get("focus_direction"))
    flow_bias = _norm(ctx.get("flow_bias"), "").lower()
    if asset in {"BTC", "ETH"}:
        reasons.append("major_asset_btc_eth")
    if direction == "long" and flow_bias in {"bullish", "neutral", "unknown", ""}:
        reasons.append("flow_not_opposed")
    if bool(ctx.get("market_override_detected") or ctx.get("market_override")):
        reasons.append("market_override_detected")
    if ctx.get("higher_timeframe_alignment") in {True, "true", "bullish", "aligned"}:
        reasons.append("higher_timeframe_alignment")
    day_direction = _day_bias_to_direction(
        ctx.get("day_preferred_direction")
        or ctx.get("day_bias")
        or ctx.get("day_regime")
        or ctx.get("price_regime")
    )
    if day_direction and day_direction == direction:
        reasons.append("day_direction_aligned")
    explicit = candidate.get("explicit_regime_flip_reason") or ctx.get("explicit_regime_flip_reason") or ctx.get("regime_flip_reason")
    if explicit:
        reasons.append(str(explicit))
    return sorted(set(reasons))


def determine_execution_mode(candidate: dict, gate_context: dict) -> dict:
    direction = _normalize_direction(candidate.get("direction"))
    event_risk_level = _norm(gate_context.get("event_risk_level"), "unknown").lower()
    event_bias = _norm(gate_context.get("event_bias"), "unknown").lower()
    shock = _risk_shock_state(gate_context)
    conflict = direction_conflicts_event_bias(direction, event_bias)
    reasons = _regime_confirmation_reasons(candidate, gate_context)
    confirm_only = _is_confirm_only_candidate(candidate)
    tactical = _counter_risk_tactical_requirements(candidate, gate_context) if candidate.get("_contract_candidate") else None
    mode = "NORMAL"
    blocked_reason = None
    if event_risk_level == "severe" and event_bias == "risk_off" and conflict:
        if shock["immediate_shock_window"]:
            mode = "BLOCKED"
            blocked_reason = "immediate_shock_counter_risk"
        elif candidate.get("explicit_regime_flip_reason") or gate_context.get("explicit_regime_flip_reason") or gate_context.get("regime_flip_reason"):
            mode = "TACTICAL_CONFIRM_ONLY" if confirm_only else "BLOCKED"
            blocked_reason = None if confirm_only else "counter_risk_requires_wait_confirm"
        elif tactical is not None and tactical["tactical_counter_risk_allowed"]:
            mode = "TACTICAL_CONFIRM_ONLY"
        elif tactical is not None:
            mode = "BLOCKED"
            blocked_reason = "counter_risk_strict_confirmation_failed"
        elif confirm_only and len(reasons) >= 2:
            mode = "TACTICAL_CONFIRM_ONLY"
        else:
            mode = "BLOCKED"
            blocked_reason = "counter_risk_without_regime_confirmation"
    return {
        "execution_mode": mode,
        "risk_size_mode": "REDUCED" if mode == "TACTICAL_CONFIRM_ONLY" else "STANDARD",
        "regime_confirmation_reasons": reasons,
        "entry_mode_required": "WAIT_CONFIRM" if mode == "TACTICAL_CONFIRM_ONLY" else None,
        "blocked_reason": blocked_reason,
        "candidate_confirm_only": confirm_only,
        **(tactical or {
            "tactical_counter_risk_allowed": mode == "TACTICAL_CONFIRM_ONLY",
            "tactical_counter_risk_failed_reasons": [],
            "post_headline_reaction_observed": not shock["immediate_shock_window"],
            "structure_invalidated": False,
            "conditions_to_eligibility": [],
        }),
        **shock,
    }


def day_execution_policy_conflict(ctx: dict) -> dict:
    day_regime = str(ctx.get("day_regime") or ctx.get("price_regime") or "").strip().lower()
    day_focus = ctx.get("day_focus") or ctx.get("day_candidates") or ctx.get("focus_asset")
    day_direction = _day_bias_to_direction(
        ctx.get("day_preferred_direction")
        or ctx.get("preferred_direction")
        or ctx.get("day_bias")
        or day_regime
    )
    downstream = str(ctx.get("execution_mode") or "").strip().upper()
    blocking = str(ctx.get("reason") or ",".join(ctx.get("hard_block_reasons") or [])).strip()
    severe_only = bool(
        downstream == "BLOCKED"
        and "risk" in blocking.lower()
        and not any(token in blocking.lower() for token in ("macro", "technical", "no_setup", "chaotic"))
    )
    conflict = bool(day_regime in {"risk_on", "bullish"} and day_direction == "long" and severe_only)
    return {
        "day_execution_policy_conflict": conflict,
        "day_regime": day_regime,
        "day_focus": day_focus,
        "day_preferred_direction": day_direction,
        "downstream_execution_mode": downstream or "unknown",
        "blocking_reason": blocking,
    }


def evaluate_post_generation_event_risk_gate(candidate: dict, gate_context: dict) -> dict:
    direction = _normalize_direction(candidate.get("direction"))
    event_risk_level = _norm(gate_context.get("event_risk_level"), "unknown").lower()
    event_bias = _norm(gate_context.get("event_bias"), "unknown").lower()
    explicit_regime_flip_reason = (
        candidate.get("explicit_regime_flip_reason")
        or gate_context.get("explicit_regime_flip_reason")
    )
    execution = determine_execution_mode(candidate, gate_context)
    blocked = execution["execution_mode"] == "BLOCKED"
    return {
        "post_generation_event_risk_gate": True,
        "candidate_direction": direction,
        "event_bias": event_bias,
        "event_risk_level": event_risk_level,
        "explicit_regime_flip_reason": explicit_regime_flip_reason,
        "can_publish_full_signal": not blocked,
        "blocked_reason": execution.get("blocked_reason"),
        "publication_type": "blocked_by_severe_risk" if blocked else "tactical_confirm_signal" if execution["execution_mode"] == "TACTICAL_CONFIRM_ONLY" else "full_signal",
        **execution,
    }


def _apply_tactical_payload_constraints(payload: dict, execution: dict) -> int:
    ttl_candidates = [
        _try_float(payload.get("confirm_timeout_minutes")),
        _try_float(payload.get("validity_minutes")),
        _try_float(payload.get("max_valid_minutes")),
    ]
    existing = min((int(value) for value in ttl_candidates if value is not None and value > 0), default=180)
    ttl = min(existing, 180)
    payload["entry_mode"] = "wait_confirm"
    payload["confirm_timeout_minutes"] = ttl
    payload["validity_minutes"] = ttl
    payload["max_valid_minutes"] = ttl
    payload["execution_mode"] = "TACTICAL_CONFIRM_ONLY"
    payload["risk_size_mode"] = "REDUCED"
    payload["automatic_entry_allowed"] = False
    payload["event_bias"] = execution.get("event_bias") or payload.get("event_bias")
    return ttl


def render_tactical_confirm_prefix(candidate: dict, execution: dict, ttl_minutes: int) -> str:
    counter = direction_conflicts_event_bias(candidate.get("direction"), execution.get("event_bias"))
    return "\n".join(
        [
            "⚠️ TACTICAL_CONFIRM_ONLY",
            "Severe headline background remains active; the first reaction window has elapsed without a confirmed structural break.",
            "Direction is counter to event bias." if counter else "Direction is aligned with event bias.",
            "Entry: WAIT_CONFIRM only after the stated strict reclaim/hold; no automatic entry.",
            "Risk: REDUCED. No chase.",
            f"TTL: {ttl_minutes} minutes.",
            "Cancel immediately on structural failure or a new material escalation.",
            "",
        ]
    )


def evaluate_aia_gate(ctx: dict, cfg: SchedulerConfig) -> dict:
    status = _norm(ctx.get("status"), "unknown").upper()
    preferred_mode = _norm(ctx.get("preferred_mode"), "unknown").lower()
    event_risk_level = _norm(ctx.get("event_risk_level"), "unknown").lower()
    event_bias = _norm(ctx.get("event_bias"), "unknown").lower()
    headline_risk_delta = _norm(ctx.get("headline_risk_delta"), "NONE").upper()
    confirm_policy = _norm(ctx.get("confirm_policy"), "unknown").lower()
    asset, direction = _candidate_from_context(ctx)
    reasons: list[str] = []

    if cfg.gate_mode == "strict" and status == "AVOID":
        reasons.append("aia_status_avoid")

    candidate_conflict = direction_conflicts_event_bias(direction, event_bias)
    pseudo_candidate = {
        "asset": asset,
        "direction": direction,
        "entry_mode": ctx.get("entry_mode") or ctx.get("candidate_entry_mode") or "wait_confirm",
        "explicit_regime_flip_reason": ctx.get("explicit_regime_flip_reason"),
    }
    execution = determine_execution_mode(pseudo_candidate, ctx)
    if (
        event_risk_level == "severe"
        and confirm_policy == "block_stale_confirm"
        and candidate_conflict
        and execution["execution_mode"] == "BLOCKED"
    ):
        reasons.append(execution.get("blocked_reason") or "severe_block_stale_confirm_conflicts_event_bias")

    dominant = ctx.get("dominant_critical_topic")
    critical_topics = ctx.get("critical_topics")
    critical_active = bool(dominant) or bool(critical_topics)
    if critical_active and candidate_conflict and execution["execution_mode"] == "BLOCKED":
        reasons.append("critical_topic_conflicts_event_bias")

    if event_bias == "risk_off" and is_risk_on_alt_long(asset, direction) and execution["execution_mode"] == "BLOCKED":
        reasons.append("risk_off_alt_long_without_reset_reclaim")

    raw_day_bias = (
        ctx.get("day_bias")
        or ctx.get("day_bias_direction")
        or (ctx.get("day_mid_context", {}) if isinstance(ctx.get("day_mid_context"), dict) else {}).get("day_bias")
    )
    day_bias_direction = _day_bias_to_direction(raw_day_bias)
    day_bias_conflict = bool(day_bias_direction and direction and day_bias_direction != direction)
    regime_flip_reason = (
        ctx.get("explicit_regime_flip_reason")
        or ctx.get("regime_flip_reason")
        or ctx.get("counter_regime_allowed_reason")
        or (ctx.get("day_mid_context", {}) if isinstance(ctx.get("day_mid_context"), dict) else {}).get("explicit_regime_flip_reason")
        or (ctx.get("day_mid_context", {}) if isinstance(ctx.get("day_mid_context"), dict) else {}).get("regime_flip_reason")
        or (ctx.get("day_mid_context", {}) if isinstance(ctx.get("day_mid_context"), dict) else {}).get("counter_regime_allowed_reason")
    )
    explicit_regime_flip_reason = (
        ctx.get("explicit_regime_flip_reason")
        or (ctx.get("day_mid_context", {}) if isinstance(ctx.get("day_mid_context"), dict) else {}).get("explicit_regime_flip_reason")
    )
    if event_risk_level == "severe" and headline_risk_delta == "ESCALATION" and candidate_conflict and not regime_flip_reason:
        reasons.append("counter_trend_signal_during_escalation_without_regime_flip")
    if day_bias_conflict and not regime_flip_reason:
        reasons.append("counter_day_bias_without_regime_flip")

    if cfg.gate_mode == "off":
        hard_block_reasons: list[str] = []
    else:
        hard_block_reasons = reasons

    selected_mode = cfg.signal_default_mode
    selected_mode_source = "scheduled_default"
    preferred_mode_ignored_reason = ""
    preferred_mode_downgrade_enabled = bool(getattr(cfg, "preferred_mode_downgrade_enabled", cfg.soft_avoid_downgrade))
    aia_avoid_soft_allowed = False
    soft_avoid_downgrade = False
    reason = "allowed"
    if cfg.gate_mode == "soft" and not hard_block_reasons:
        selected_mode_source = "scheduled_default_soft_no_hard_block"
        if preferred_mode in {"neutral", "conservative"}:
            preferred_mode_ignored_reason = "soft_gate_no_hard_block"
        if status == "AVOID":
            aia_avoid_soft_allowed = True
            reason = "avoid_without_hard_block_keep_default_mode"
        if preferred_mode_downgrade_enabled and preferred_mode in {"neutral", "conservative"} and cfg.signal_default_mode == "aggressive":
            selected_mode = preferred_mode
            selected_mode_source = "aia_preferred_mode_soft_downgrade"
            preferred_mode_ignored_reason = ""
            soft_avoid_downgrade = True
            reason = "avoid_without_hard_block_downgrade_preferred_mode"
    elif preferred_mode in {"neutral", "conservative"} and cfg.signal_default_mode == "aggressive":
        selected_mode = preferred_mode
        selected_mode_source = "aia_preferred_mode"

    return {
        "allowed": not hard_block_reasons,
        "reason": reason if not hard_block_reasons else ",".join(hard_block_reasons),
        "selected_mode": selected_mode if selected_mode in {"aggressive", "neutral", "conservative"} else "aggressive",
        "selected_mode_source": selected_mode_source,
        "preferred_mode_downgrade_enabled": preferred_mode_downgrade_enabled,
        "preferred_mode_ignored_reason": preferred_mode_ignored_reason,
        "aia_avoid_soft_allowed": aia_avoid_soft_allowed,
        "soft_avoid_downgrade": soft_avoid_downgrade,
        "hard_block_reasons": hard_block_reasons,
        "aia_status": status,
        "preferred_mode": preferred_mode if preferred_mode in {"aggressive", "neutral", "conservative"} else "unknown",
        "conservative_candidate": preferred_mode == "conservative",
        "conservative_reason": selected_mode_source if preferred_mode == "conservative" else "",
        "conservative_quality_score": ctx.get("conservative_quality_score"),
        "higher_timeframe_alignment": ctx.get("higher_timeframe_alignment"),
        "focus_asset": asset,
        "focus_direction": direction,
        "flow_bias": _norm(ctx.get("flow_bias"), "unknown").lower(),
        "execution_bias": ctx.get("execution_bias"),
        "focus_before_risk": ctx.get("focus_before_risk") or asset,
        "focus_after_risk": ctx.get("focus_after_risk") or asset,
        "focus_status": ctx.get("focus_status"),
        "focus_preserved": ctx.get("focus_preserved"),
        "focus_reset_reason": ctx.get("focus_reset_reason"),
        "event_risk_level": event_risk_level,
        "headline_risk_delta": headline_risk_delta,
        "previous_risk_level": ctx.get("previous_risk_level"),
        "new_risk_level": ctx.get("new_risk_level"),
        "risk_transition_reason": ctx.get("risk_transition_reason"),
        "duplicate_decision": ctx.get("duplicate_decision"),
        "headline_update_generated": bool(ctx.get("headline_update_generated")),
        "dominant_critical_topic": dominant.get("topic_id") if isinstance(dominant, dict) else _norm(dominant, ""),
        "event_bias": event_bias,
        "confirm_policy": confirm_policy,
        "aia_context_missing": bool(ctx.get("aia_context_missing")),
        "explicit_regime_flip_reason": explicit_regime_flip_reason,
        "execution_mode": execution["execution_mode"] if not hard_block_reasons else "BLOCKED",
        "risk_size_mode": execution["risk_size_mode"],
        "regime_confirmation_reasons": execution["regime_confirmation_reasons"],
        "entry_mode_required": execution["entry_mode_required"],
        "immediate_shock_window": execution["immediate_shock_window"],
        "background_risk_active": execution["background_risk_active"],
        "material_event_at": execution["material_event_at"],
        "last_material_change_at": execution["last_material_change_at"],
        "snapshot_built_at": execution["snapshot_built_at"],
        "immediate_shock_until": execution["immediate_shock_until"],
        "post_headline_reaction_observed": execution.get("post_headline_reaction_observed"),
        "structure_invalidated": execution.get("structure_invalidated"),
        "tactical_counter_risk_allowed": execution.get("tactical_counter_risk_allowed"),
        "tactical_counter_risk_failed_reasons": execution.get("tactical_counter_risk_failed_reasons"),
        "conditions_to_eligibility": execution.get("conditions_to_eligibility"),
    }


class TelegramBotAdapter:
    def __init__(self) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        from telegram import Bot

        self._bot = Bot(token=token)

    async def send_message(self, **kwargs):
        return await self._bot.send_message(**kwargs)

    async def pin_chat_message(self, **kwargs):
        return await self._bot.pin_chat_message(**kwargs)

    async def unpin_chat_message(self, **kwargs):
        return await self._bot.unpin_chat_message(**kwargs)


class SchedulerBot:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.message_id = 1000000
        self.sent: list[dict] = []
        self._real = None if dry_run else TelegramBotAdapter()

    async def send_message(self, **kwargs):
        if self.dry_run:
            self.message_id += 1
            row = dict(kwargs)
            row["message_id"] = self.message_id
            self.sent.append(row)
            return SimpleNamespace(message_id=self.message_id)
        return await self._real.send_message(**kwargs)

    async def pin_chat_message(self, **kwargs):
        if self.dry_run:
            return True
        return await self._real.pin_chat_message(**kwargs)

    async def unpin_chat_message(self, **kwargs):
        if self.dry_run:
            return True
        return await self._real.unpin_chat_message(**kwargs)


def make_context(dry_run: bool = False):
    return SimpleNamespace(bot=SchedulerBot(dry_run=dry_run))


def run_command(cmd: list[str], *, env: dict | None = None, timeout: int = 1200, dry_run: bool = False):
    if dry_run:
        return SimpleNamespace(returncode=0, stdout="[dry-run]", stderr="")
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(cmd, cwd=PROJECT_ROOT, env=merged_env, capture_output=True, text=True, timeout=timeout)


def extract_report_dir(kind: str, proc) -> Path | None:
    output = "\n".join(part for part in (getattr(proc, "stdout", ""), getattr(proc, "stderr", "")) if part)
    marker = f"✅ {kind.upper()} report:"
    for line in reversed(output.splitlines()):
        if line.startswith(marker):
            raw = line.split(":", 1)[1].strip()
            path = Path(raw)
            return path if path.is_absolute() else PROJECT_ROOT / path
    root = PROJECT_ROOT / "reports" / kind
    dirs = [p for p in root.glob("*") if p.is_dir() and not p.name.startswith(".tmp_")]
    return sorted(dirs)[-1] if dirs else None


def scheduled_report_targets(kind: str, cfg: SchedulerConfig) -> list[int]:
    return list(dict.fromkeys([*cfg.target_chat_ids, *report_end_user_chat_ids(kind)]))


async def publish_report(
    kind: str,
    cfg: SchedulerConfig,
    *,
    dry_run: bool = False,
    target_chat_ids: list[int] | None = None,
    report_dir: Path | None = None,
) -> dict:
    import tg_bot

    if report_dir is None:
        script = f"./run_{kind}.sh"
        proc = run_command(["bash", "-lc", f"chmod +x {script} && {script}"], timeout=1200, dry_run=dry_run)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError((getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or f"{script} failed").strip())
        report_dir = extract_report_dir(kind, proc)
    if not report_dir:
        raise RuntimeError(f"{kind.upper()} report artifact not found")
    context = make_context(dry_run=dry_run)
    emoji = "🗓" if kind == "day" else "📰"
    message_ids: list[int] = []
    successful_targets: list[int] = []
    target_errors: dict[str, str] = {}
    report_targets = list(target_chat_ids) if target_chat_ids is not None else scheduled_report_targets(kind, cfg)
    for channel in report_targets:
        before = len(context.bot.sent) if dry_run else 0
        try:
            await tg_bot._post_report(kind, emoji, context, channel, report_dir=report_dir)
            successful_targets.append(channel)
            if dry_run:
                message_ids.extend(item["message_id"] for item in context.bot.sent[before:])
        except Exception as exc:
            target_errors[str(channel)] = str(exc)
    return {
        "artifact_path": relpath(report_dir),
        "message_id": message_ids[0] if message_ids else None,
        "target_chat_ids": successful_targets,
        "target_errors": target_errors,
    }


def read_last_signal_payload() -> dict:
    return read_json(PROJECT_ROOT / "logs/last.json")


async def forward_signal_to_aia_awaited(tg_bot, signal_json_v1: dict | None) -> dict:
    result = {
        "aia_forward_attempted": False,
        "aia_forward_ok": False,
        "aia_forward_error": None,
        "aia_forward_mode": "awaited_scheduled",
    }
    if not signal_json_v1:
        result["aia_forward_error"] = "payload_build_failed"
        return result

    result["aia_forward_attempted"] = True
    try:
        ok = bool(await asyncio.to_thread(tg_bot.send_signal_to_aia, signal_json_v1))
    except Exception as exc:
        result["aia_forward_error"] = str(exc)
        return result
    result["aia_forward_ok"] = ok
    if not ok:
        result["aia_forward_error"] = "send_signal_to_aia returned false"
    return result


async def generate_and_publish_signal(
    selected_mode: str,
    cfg: SchedulerConfig,
    *,
    dry_run: bool = False,
    now_utc: datetime | None = None,
    gate: dict | None = None,
) -> dict:
    import tg_bot

    gate_context = gate if isinstance(gate, dict) else {}
    duplicate_decision = {"publication_type": "full_signal", "duplicate_in_work_signal_detected": False}
    env = {"FORCE_MODE": selected_mode, "SIGNAL_SKIP_AIA_SEND": "1"}
    proc = run_command(["bash", "-lc", "./signal full"], env=env, timeout=1200, dry_run=dry_run)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError((getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "signal full failed").strip())
    sig_html, run_log = tg_bot._resolve_signal_run_artifacts(proc)
    payload = read_last_signal_payload()
    if bool(payload.get("no_trade")):
        return {
            "published": False,
            "reason": "signal_core_no_trade",
            "signal_id": None,
            "artifact_path": relpath(Path(run_log)) if run_log else None,
            "last_payload": payload,
        }
    if not sig_html:
        return {
            "published": False,
            "reason": "no_valid_signal_candidate",
            "signal_id": None,
            "artifact_path": relpath(Path(run_log)) if run_log else None,
            "last_payload": payload,
        }
    parts = tg_bot.html_file_to_tg_text(Path(sig_html))
    if not parts:
        return {
            "published": False,
            "reason": "no_valid_signal_candidate",
            "signal_id": None,
            "artifact_path": relpath(Path(sig_html)),
            "last_payload": payload,
            **duplicate_decision,
        }

    published_at = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    signal_id = tg_bot._infer_signal_id(Path(sig_html), Path(run_log) if run_log else None, published_at)
    candidate = _candidate_from_payload(payload, signal_id=signal_id)
    post_generation_gate = evaluate_post_generation_event_risk_gate(candidate, gate_context)
    tp_validation = _tp_ladder_validation(
        candidate.get("direction"),
        candidate.get("entry_price"),
        candidate.get("tp1"),
        candidate.get("tp2"),
        candidate.get("tp3"),
    )
    state_guard_shadow = evaluate_state_guard_shadow(candidate, cfg)
    state_guard_runtime = state_guard_duplicate_runtime_action(state_guard_shadow, cfg)
    state_guard_enforcement = build_state_guard_enforcement_decision(state_guard_shadow, candidate, cfg)
    day_diagnostics = day_bias_diagnostics(payload, candidate, gate={})
    if str(state_guard_shadow.get("state_guard_decision") or "").upper() == "OPPOSITE_BIAS_BEFORE_ENTRY":
        decision = {
            "publication_type": "urgent_review",
            "duplicate_signal_id": state_guard_shadow.get("state_guard_old_signal_id")
            or state_guard_shadow.get("state_guard_primary_signal_id"),
            "duplicate_signal_status": state_guard_shadow.get("state_guard_primary_lifecycle_state"),
            "replacement_reason": "opposite_bias_before_entry",
            "entry_blocked": True,
            "human_decision_required": True,
            "old_setup_recommendation": state_guard_shadow.get("state_guard_old_setup_recommendation")
            or "WAIT_HUMAN_DECISION",
            "propose_reverse_signal": bool(state_guard_shadow.get("state_guard_propose_reverse_signal")),
            "duplicate_signal": {
                "signal_id": state_guard_shadow.get("state_guard_old_signal_id")
                or state_guard_shadow.get("state_guard_primary_signal_id"),
                "status": state_guard_shadow.get("state_guard_primary_lifecycle_state"),
                "display_symbol": candidate.get("display_symbol"),
                "direction": state_guard_shadow.get("state_guard_old_direction"),
            },
        }
        if not dry_run and decision["duplicate_signal_id"]:
            emit_opposite_bias_before_entry(
                now_utc=now_utc or utc_now(),
                old_signal_id=str(decision["duplicate_signal_id"]),
                old_direction=state_guard_shadow.get("state_guard_old_direction"),
                candidate_signal_id=signal_id,
                new_direction=candidate.get("direction"),
                symbol=candidate.get("display_symbol") or candidate.get("symbol"),
                recommendation=decision["old_setup_recommendation"],
                propose_reverse_signal=decision["propose_reverse_signal"],
            )
        message_ids = await publish_plain_message(cfg, render_duplicate_update_message(decision, candidate), dry_run=dry_run)
        return {
            "published": True,
            "reason": "opposite_bias_before_entry",
            "signal_id": None,
            "candidate_signal_id": signal_id,
            "artifact_path": relpath(Path(sig_html)),
            "run_log": relpath(Path(run_log)) if run_log else None,
            "last_payload": payload,
            "message_ids": message_ids,
            "publication_type": "urgent_review",
            "duplicate_in_work_signal_detected": True,
            "duplicate_signal_id": decision["duplicate_signal_id"],
            "duplicate_signal_status": decision["duplicate_signal_status"],
            "entry_blocked": True,
            "human_decision_required": True,
            "old_setup_recommendation": decision["old_setup_recommendation"],
            "propose_reverse_signal": decision["propose_reverse_signal"],
            "aia_forward_attempted": False,
            "aia_forward_ok": False,
            "aia_forward_error": None,
            "aia_forward_mode": "skipped_pre_entry_opposite_bias",
            "aia_forward_warning": None,
            **state_guard_shadow,
            **state_guard_runtime,
            **state_guard_enforcement,
            **day_diagnostics,
        }
    if state_guard_enforcement.get("state_guard_runtime_blocked_full_signal"):
        publication_type = str(state_guard_enforcement.get("publication_type") or "active_signal_update").strip().lower()
        decision = {
            "publication_type": publication_type,
            "duplicate_signal_id": state_guard_enforcement.get("duplicate_signal_id"),
            "duplicate_signal_status": state_guard_enforcement.get("duplicate_signal_status"),
            "replacement_reason": state_guard_enforcement.get("replacement_reason"),
            "duplicate_signal": state_guard_enforcement.get("duplicate_signal"),
            "related_signal_id": state_guard_enforcement.get("related_signal_id"),
            "refresh_of_signal_id": state_guard_enforcement.get("refresh_of_signal_id"),
            "replaces_signal_id": state_guard_enforcement.get("replaces_signal_id"),
            "previous_entry": state_guard_enforcement.get("previous_entry"),
            "new_entry": state_guard_enforcement.get("new_entry"),
            "previous_lifecycle_state": state_guard_enforcement.get("previous_lifecycle_state"),
            "pending_update_classification": state_guard_enforcement.get("pending_update_classification"),
            "entry_distance_pct": state_guard_enforcement.get("entry_distance_pct"),
        }
        for key in (
            "event_risk_level",
            "event_bias",
            "dominant_critical_topic",
            "critical_topics",
            "confirm_policy",
            "headline_risk_delta",
            "previous_risk_level",
            "new_risk_level",
            "risk_transition_reason",
            "duplicate_decision",
            "headline_update_generated",
        ):
            if gate_context.get(key) not in (None, "", [], {}):
                decision[key] = gate_context.get(key)
        message = render_duplicate_update_message(decision, candidate)
        message_ids = await publish_plain_message(cfg, message, dry_run=dry_run)
        return {
            "published": True,
            "reason": decision["replacement_reason"],
            "signal_id": None,
            "candidate_signal_id": signal_id,
            "artifact_path": relpath(Path(sig_html)),
            "run_log": relpath(Path(run_log)) if run_log else None,
            "last_payload": payload,
            "message_ids": message_ids,
            "publication_type": publication_type,
            "duplicate_in_work_signal_detected": True,
            "duplicate_signal_id": decision["duplicate_signal_id"],
            "duplicate_signal_status": decision["duplicate_signal_status"],
            "replacement_reason": decision["replacement_reason"],
            **tp_validation,
            "aia_forward_attempted": False,
            "aia_forward_ok": False,
            "aia_forward_error": None,
            "aia_forward_mode": "skipped_state_guard_enforcement",
            "aia_forward_warning": None,
            **state_guard_shadow,
            **state_guard_runtime,
            **state_guard_enforcement,
            **day_diagnostics,
        }
    duplicate_decision = evaluate_duplicate_publication(candidate, load_in_work_signal_state(now_utc), now_utc=now_utc)
    duplicate_result = {k: v for k, v in duplicate_decision.items() if k != "duplicate_signal"}

    if duplicate_decision.get("publication_type") in {"active_signal_update", "conflict_update", "urgent_review"}:
        if duplicate_decision.get("publication_type") == "urgent_review" and not dry_run:
            old = duplicate_decision.get("duplicate_signal") or {}
            emit_opposite_bias_before_entry(
                now_utc=now_utc or utc_now(),
                old_signal_id=str(duplicate_decision.get("duplicate_signal_id")),
                old_direction=old.get("direction"),
                candidate_signal_id=signal_id,
                new_direction=candidate.get("direction"),
                symbol=candidate.get("display_symbol") or candidate.get("symbol"),
                recommendation=duplicate_decision.get("old_setup_recommendation") or "WAIT_HUMAN_DECISION",
                propose_reverse_signal=bool(duplicate_decision.get("propose_reverse_signal")),
            )
        message = render_duplicate_update_message(duplicate_decision, candidate)
        message_ids = await publish_plain_message(cfg, message, dry_run=dry_run)
        return {
            "published": True,
            "reason": str(duplicate_decision.get("replacement_reason") or "duplicate_in_work_signal"),
            "signal_id": None,
            "candidate_signal_id": signal_id,
            "artifact_path": relpath(Path(sig_html)),
            "run_log": relpath(Path(run_log)) if run_log else None,
            "last_payload": payload,
            "message_ids": message_ids,
            "aia_forward_attempted": False,
            "aia_forward_ok": False,
            "aia_forward_error": None,
            "aia_forward_mode": "skipped_duplicate_update",
            "aia_forward_warning": None,
            **tp_validation,
            **state_guard_shadow,
            **state_guard_runtime,
            **state_guard_enforcement,
            **day_diagnostics,
            **duplicate_result,
        }

    if not post_generation_gate["can_publish_full_signal"]:
        return {
            "published": False,
            "reason": post_generation_gate.get("blocked_reason") or "post_generation_event_risk_gate",
            "signal_id": None,
            "candidate_signal_id": signal_id,
            "artifact_path": relpath(Path(sig_html)),
            "run_log": relpath(Path(run_log)) if run_log else None,
            "last_payload": payload,
            "aia_forward_attempted": False,
            "aia_forward_ok": False,
            "aia_forward_error": None,
            "aia_forward_mode": "skipped_post_generation_event_risk_gate",
            "aia_forward_warning": None,
            **post_generation_gate,
        }

    tactical_ttl_minutes = None
    if post_generation_gate.get("execution_mode") == "TACTICAL_CONFIRM_ONLY":
        tactical_ttl_minutes = _apply_tactical_payload_constraints(payload, post_generation_gate)

    publish_text = parts[0]
    if duplicate_decision.get("publication_type") == "replace_wait_confirm":
        publish_text = render_replacement_prefix(duplicate_decision, candidate) + parts[0]
    elif (
        str(state_guard_shadow.get("state_guard_decision") or "").upper() == "RE_ENTRY_SIGNAL"
        and state_guard_shadow.get("state_guard_reentry_allowed") is True
    ):
        publish_text = (
            tg_bot.render_state_guard_classification_message(
                {"decision": "RE_ENTRY_SIGNAL", "symbol": candidate.get("display_symbol"), "direction": candidate.get("direction")},
                symbol=candidate.get("display_symbol"),
            )
            + "\n\n"
            + parts[0]
        )
    if post_generation_gate.get("execution_mode") == "TACTICAL_CONFIRM_ONLY":
        publish_text = render_tactical_confirm_prefix(candidate, post_generation_gate, int(tactical_ttl_minutes or 180)) + publish_text

    old_get_targets = tg_bot.get_main_publication_targets
    old_get_chat = tg_bot.get_main_publication_chat_id
    old_get_mode = tg_bot.get_user_mode
    full_signal_targets = scheduled_full_signal_targets(cfg)
    try:
        tg_bot.get_main_publication_targets = lambda uid: full_signal_targets.copy()
        tg_bot.get_main_publication_chat_id = lambda uid: full_signal_targets[0] if full_signal_targets else None
        tg_bot.get_user_mode = lambda uid: selected_mode
        if full_signal_targets:
            context = make_context(dry_run=dry_run)
            ok = await tg_bot._publish_signal_result(
                context,
                SCHEDULER_UID,
                text=publish_text,
                target_chat_id=full_signal_targets[0],
                delivery_kind="main",
                source="scheduled_runner.py:generate_and_publish_signal",
                symbol_hint=None,
                sig_html=Path(sig_html),
                run_log=Path(run_log) if run_log else None,
                skip_aia_forward=True,
            )
        else:
            ok = True
    finally:
        tg_bot.get_main_publication_targets = old_get_targets
        tg_bot.get_main_publication_chat_id = old_get_chat
        tg_bot.get_user_mode = old_get_mode

    aia_forward = {
        "aia_forward_attempted": False,
        "aia_forward_ok": False,
        "aia_forward_error": None,
        "aia_forward_mode": "awaited_scheduled",
    }
    if ok:
        if tp_validation["invalid_tp_ladder"]:
            LOGGER.warning(
                "tp ladder validation warning",
                extra={
                    "signal_id": signal_id,
                    "symbol": candidate.get("display_symbol"),
                    "direction": candidate.get("direction"),
                    "tp_ladder_warnings": tp_validation["tp_ladder_warnings"],
                },
            )
        try:
            tg_bot._AIA_UID_CONTEXT = SCHEDULER_UID
        except Exception:
            pass
        signal_json_v1 = tg_bot._build_signal_json_v1(
            signal_id=signal_id,
            published_at=published_at,
            channel_id=full_signal_targets[0] if full_signal_targets else None,
            origin_chat_id=full_signal_targets[0] if full_signal_targets else None,
            publish_targets=full_signal_targets.copy(),
            symbol_hint=None,
            last_payload=payload,
            last_json_path=PROJECT_ROOT / "logs/last.json",
        )
        if signal_json_v1 and all(key in signal_json_v1 for key in ("symbol", "direction", "published_at")):
            signal_json_v1.update(
                {
                    "signal_origin": "scheduled",
                    "signal_origin_type": "SCHEDULED",
                    "owner_profile_id": None,
                    "lifecycle_targets": full_signal_targets.copy(),
                }
            )
            for requester_key in (
                "origin_user_id",
                "requester_telegram_user_id",
                "requester_chat_id",
                "requester_profile_id",
                "requested_at",
                "request_correlation_id",
            ):
                signal_json_v1.pop(requester_key, None)
        if signal_json_v1 and duplicate_decision.get("publication_type") == "replace_wait_confirm":
            signal_json_v1["publication_type"] = "replace_wait_confirm"
            signal_json_v1["replaced_signal_id"] = duplicate_decision.get("replaced_signal_id")
            signal_json_v1["replacement_reason"] = duplicate_decision.get("replacement_reason")
            if not dry_run:
                emit_replacement_event(duplicate_decision, candidate)
        aia_forward = await forward_signal_to_aia_awaited(tg_bot, signal_json_v1)
    return {
        "published": bool(ok),
        "reason": "published" if ok else "system_routing_api_error",
        "signal_id": signal_id if ok else None,
        "candidate_signal_id": signal_id,
        "artifact_path": relpath(Path(sig_html)),
        "run_log": relpath(Path(run_log)) if run_log else None,
        "last_payload": payload,
        **aia_forward,
        "aia_forward_warning": "aia_forward_failed" if ok and not aia_forward.get("aia_forward_ok") else None,
        **tp_validation,
        **state_guard_shadow,
        **state_guard_runtime,
        **state_guard_enforcement,
        **day_diagnostics,
        **duplicate_result,
        "post_generation_event_risk_gate": True,
        "publication_type": post_generation_gate.get("publication_type") or "full_signal",
        "can_publish_full_signal": post_generation_gate.get("can_publish_full_signal"),
        "execution_mode": post_generation_gate.get("execution_mode"),
        "risk_size_mode": post_generation_gate.get("risk_size_mode"),
        "entry_mode_required": post_generation_gate.get("entry_mode_required"),
        "regime_confirmation_reasons": post_generation_gate.get("regime_confirmation_reasons"),
        "candidate_confirm_only": post_generation_gate.get("candidate_confirm_only"),
        "immediate_shock_window": post_generation_gate.get("immediate_shock_window"),
        "immediate_shock_until": post_generation_gate.get("immediate_shock_until"),
        "background_risk_active": post_generation_gate.get("background_risk_active"),
        "material_event_at": post_generation_gate.get("material_event_at"),
        "last_material_change_at": post_generation_gate.get("last_material_change_at"),
        "post_headline_reaction_observed": post_generation_gate.get("post_headline_reaction_observed"),
        "structure_invalidated": post_generation_gate.get("structure_invalidated"),
        "tactical_counter_risk_allowed": post_generation_gate.get("tactical_counter_risk_allowed"),
        "tactical_counter_risk_failed_reasons": post_generation_gate.get("tactical_counter_risk_failed_reasons"),
        "conditions_to_eligibility": post_generation_gate.get("conditions_to_eligibility"),
        "confirmation_ttl_minutes": tactical_ttl_minutes,
        "explicit_regime_flip_reason": post_generation_gate.get("explicit_regime_flip_reason"),
        "post_generation_event_risk_blocked_reason": post_generation_gate.get("blocked_reason"),
        "channel_mode": "WINDOW_ONLY" if dima_window_only(cfg) else "FULL_SIGNAL",
        "original_publication_type": duplicate_result.get("publication_type") or "full_signal",
        "window_message_sent": False,
        "lifecycle_created": False if dima_window_only(cfg) else bool(full_signal_targets and aia_forward.get("aia_forward_attempted")),
        "dima_manual_signal_preserved": True,
        "scheduled_full_signal_targets": full_signal_targets,
    }


def base_signal_log_row(now_utc: datetime, cfg: SchedulerConfig, slot_id: str, slot_time: datetime, attempt: int, gate: dict) -> dict:
    policy_conflict = day_execution_policy_conflict(gate)
    return {
        "ts_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "slot_id": slot_id,
        "slot_time_msk": slot_time.isoformat(),
        "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "attempt": attempt,
        "decision": None,
        "reason": gate.get("reason"),
        "gate_mode": cfg.gate_mode,
        "aia_status": gate.get("aia_status", "unknown"),
        "preferred_mode": gate.get("preferred_mode", "unknown"),
        "selected_mode": gate.get("selected_mode", cfg.signal_default_mode),
        "selected_mode_source": gate.get("selected_mode_source", "unknown"),
        "preferred_mode_downgrade_enabled": bool(gate.get("preferred_mode_downgrade_enabled")),
        "preferred_mode_ignored_reason": gate.get("preferred_mode_ignored_reason", ""),
        "aia_avoid_soft_allowed": bool(gate.get("aia_avoid_soft_allowed")),
        "soft_avoid_downgrade": bool(gate.get("soft_avoid_downgrade")),
        "focus_asset": gate.get("focus_asset", "none"),
        "focus_direction": gate.get("focus_direction", "unknown"),
        "flow_bias": gate.get("flow_bias", "unknown"),
        "execution_bias": gate.get("execution_bias"),
        "focus_before_risk": gate.get("focus_before_risk"),
        "focus_after_risk": gate.get("focus_after_risk"),
        "focus_status": gate.get("focus_status"),
        "focus_preserved": gate.get("focus_preserved"),
        "focus_reset_reason": gate.get("focus_reset_reason"),
        "event_risk_level": gate.get("event_risk_level", "unknown"),
        "headline_risk_delta": gate.get("headline_risk_delta", "NONE"),
        "previous_risk_level": gate.get("previous_risk_level"),
        "new_risk_level": gate.get("new_risk_level"),
        "risk_transition_reason": gate.get("risk_transition_reason"),
        "duplicate_decision": gate.get("duplicate_decision"),
        "headline_update_generated": gate.get("headline_update_generated"),
        "dominant_critical_topic": gate.get("dominant_critical_topic", ""),
        "event_bias": gate.get("event_bias", "unknown"),
        "confirm_policy": gate.get("confirm_policy", "unknown"),
        "hard_block_reasons": gate.get("hard_block_reasons", []),
        "execution_mode": gate.get("execution_mode", "NORMAL"),
        "risk_size_mode": gate.get("risk_size_mode"),
        "regime_confirmation_reasons": gate.get("regime_confirmation_reasons", []),
        "entry_mode_required": gate.get("entry_mode_required"),
        "immediate_shock_window": gate.get("immediate_shock_window"),
        "background_risk_active": gate.get("background_risk_active"),
        "material_event_at": gate.get("material_event_at"),
        "last_material_change_at": gate.get("last_material_change_at"),
        "snapshot_built_at": gate.get("snapshot_built_at"),
        "immediate_shock_until": gate.get("immediate_shock_until"),
        "post_headline_reaction_observed": gate.get("post_headline_reaction_observed"),
        "structure_invalidated": gate.get("structure_invalidated"),
        "tactical_counter_risk_allowed": gate.get("tactical_counter_risk_allowed"),
        "tactical_counter_risk_failed_reasons": gate.get("tactical_counter_risk_failed_reasons", []),
        "conditions_to_eligibility": gate.get("conditions_to_eligibility", []),
        "target_chat_ids": cfg.target_chat_ids,
        "signal_id": None,
        "error": None,
        "aia_context_missing": bool(gate.get("aia_context_missing")),
        **policy_conflict,
    }


async def run_signal_slot(now_utc: datetime, cfg: SchedulerConfig, state: dict, slot_id: str, slot_time: datetime, attempt: int, *, dry_run: bool = False) -> None:
    slots = state.setdefault("slots", {})
    current = slots.get(slot_id)
    if isinstance(current, dict) and current.get("status") in {"published", "macro_substitution", "active_signal_update", "replace_wait_confirm", "conflict_update"}:
        gate = evaluate_aia_gate(load_aia_context(), cfg)
        row = base_signal_log_row(now_utc, cfg, slot_id, slot_time, attempt, gate)
        row.update({"decision": "duplicate_skip", "reason": f"already_{current.get('status')}", "signal_id": current.get("signal_id")})
        if not dry_run:
            add_scheduled_decision_observability(row, cfg, stage=FUNNEL_STAGE_RETRY_DEDUPED)
            append_signal_decision_with_health(now_utc, row)
        return

    gate = evaluate_aia_gate(load_aia_context(), cfg)
    row = base_signal_log_row(now_utc, cfg, slot_id, slot_time, attempt, gate)
    macro_events = load_scheduled_macro_events(now_utc)
    macro_guard = evaluate_macro_event_guard(
        now=now_utc,
        events=macro_events,
        side=str(gate.get("focus_direction") or ""),
        forced_override=False,
        state_path=PROJECT_ROOT / "logs/macro_event_state.json",
    )
    if macro_guard.get("phase") == "expired_missing_classification":
        event = macro_guard.get("event") if isinstance(macro_guard.get("event"), dict) else {}
        row.update(
            {
                "macro_event_name": event.get("event_name"),
                "macro_event_time_msk": event.get("event_time_msk"),
                "macro_phase": macro_guard.get("phase"),
                "macro_policy": macro_guard.get("macro_policy"),
                "macro_reason": macro_guard.get("reason"),
                "macro_event_age_minutes": macro_guard.get("event_age_minutes"),
                "macro_substitution_applied": False,
                "scheduled_signal_substituted": False,
                "post_event_classification_status": "missing",
            }
        )
    if macro_guard.get("active"):
        event = macro_guard.get("event") if isinstance(macro_guard.get("event"), dict) else {}
        classification = macro_guard.get("post_event_classification") if isinstance(macro_guard.get("post_event_classification"), dict) else None
        message_type = str(macro_guard.get("macro_message_type") or "MACRO_EVENT_ANALYSIS_PENDING")
        if macro_guard.get("phase") == "post_event_classified" and classification:
            if str(classification.get("execution_policy") or "") == "no_trade_chaotic" or str(classification.get("allowed_direction") or "") == "none":
                message_type = "MACRO_NO_TRADE_RECOMMENDATION"
            else:
                message_type = "MACRO_TRADE_PROPOSAL"
        dedupe_key = macro_dedupe_key(event, message_type, str(macro_guard.get("phase") or "macro"), now_utc)
        should_send = macro_message_allowed(state, dedupe_key, message_type, now_utc, classification)
        message_ids: list[int] = []
        dima_macro_audit: dict = {}
        if should_send:
            message = render_macro_event_message(message_type, event, classification)
            message_ids = await publish_macro_event_message(cfg, message, dry_run=dry_run)
            dima_macro_audit = await maybe_publish_dima_macro_window(
                cfg,
                event,
                classification,
                now_utc=now_utc,
                dry_run=dry_run,
            )
            state.setdefault("macro_dedupe", {})[dedupe_key] = {
                "sent_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
                "macro_message_type": message_type,
                "post_event_classification": classification,
            }
        slots[slot_id] = {
            "slot_id": slot_id,
            "slot_time_msk": slot_time.isoformat(),
            "attempt": attempt,
            "status": "macro_substitution",
            "next_retry_at_msk": None,
            "reason": macro_guard.get("reason") or "scheduled_signal_substituted_by_macro_event",
            "selected_mode": gate["selected_mode"],
            "signal_id": None,
            "macro_message_type": message_type,
            "macro_event_name": event.get("event_name"),
            "macro_event_time_msk": event.get("event_time_msk"),
            "macro_dedupe_key": dedupe_key,
            "post_event_classification_status": "present" if classification else "missing",
        }
        row.update(
            {
                "decision": "macro_substitution",
                "reason": macro_guard.get("reason") or "scheduled_signal_substituted_by_macro_event",
                "macro_event_name": event.get("event_name"),
                "macro_event_time_msk": event.get("event_time_msk"),
                "macro_phase": macro_guard.get("phase"),
                "macro_policy": macro_guard.get("macro_policy"),
                "macro_message_type": message_type,
                "macro_dedupe_key": dedupe_key,
                "macro_message_sent": should_send,
                "macro_message_ids": message_ids,
                "macro_substitution_applied": True,
                "scheduled_signal_substituted": True,
                "post_event_classification": classification,
                "post_event_classification_status": "present" if classification else "missing",
                "old_narrative_valid": classification.get("old_narrative_valid") if isinstance(classification, dict) else None,
                "allowed_direction": classification.get("allowed_direction") if isinstance(classification, dict) else None,
                **dima_macro_audit,
            }
        )
        if not dry_run:
            write_json_atomic(cfg.signal_state_path, state)
            add_scheduled_decision_observability(row, cfg, stage=FUNNEL_STAGE_PUBLICATION_SKIPPED)
            append_signal_decision_with_health(now_utc, row)
        return

    if not gate["allowed"]:
        dima_window_audit = await maybe_publish_dima_market_window(
            cfg,
            state,
            {**gate, "slot_id": slot_id},
            now_utc=now_utc,
            dry_run=dry_run,
        )
        row.update(dima_window_audit)
        if attempt < cfg.max_attempts:
            retry_at = slot_time + timedelta(minutes=cfg.retry_delay_minutes)
            slots[slot_id] = {
                "slot_id": slot_id,
                "slot_time_msk": slot_time.isoformat(),
                "attempt": attempt,
                "status": "deferred",
                "next_retry_at_msk": retry_at.isoformat(),
                "reason": gate["reason"],
                "selected_mode": gate["selected_mode"],
                "signal_id": None,
            }
            row.update({"decision": "defer", "reason": gate["reason"]})
        else:
            slots[slot_id] = {
                "slot_id": slot_id,
                "slot_time_msk": slot_time.isoformat(),
                "attempt": attempt,
                "status": "cancelled",
                "next_retry_at_msk": None,
                "reason": gate["reason"],
                "selected_mode": gate["selected_mode"],
                "signal_id": None,
            }
            row.update({"decision": "cancel", "reason": gate["reason"]})
        if not dry_run:
            write_json_atomic(cfg.signal_state_path, state)
            add_scheduled_decision_observability(
                row,
                cfg,
                stage=FUNNEL_STAGE_RUN_CANCELLED if row.get("decision") == "cancel" else FUNNEL_STAGE_PRE_GENERATION,
            )
            append_signal_decision_with_health(now_utc, row)
        return

    try:
        result = await generate_and_publish_signal(str(gate["selected_mode"]), cfg, dry_run=dry_run, now_utc=now_utc, gate=gate)
        dima_window_audit = await maybe_publish_dima_market_window(
            cfg,
            state,
            {**_dima_context_from_signal_result(gate, result), "slot_id": slot_id},
            now_utc=now_utc,
            dry_run=dry_run,
        )
        row.update(dima_window_audit)
        state_guard_shadow_available = "state_guard_status" in result
        if result.get("published"):
            publication_type = str(result.get("publication_type") or "full_signal")
            slots[slot_id] = {
                "slot_id": slot_id,
                "slot_time_msk": slot_time.isoformat(),
                "attempt": attempt,
                "status": "published" if publication_type == "full_signal" else publication_type,
                "next_retry_at_msk": None,
                "reason": result.get("reason"),
                "selected_mode": gate["selected_mode"],
                "signal_id": result.get("signal_id"),
                "publication_type": publication_type,
                "duplicate_signal_id": result.get("duplicate_signal_id"),
                "replaced_signal_id": result.get("replaced_signal_id"),
            }
            row.update(
                {
                    "decision": "publish" if publication_type == "full_signal" else publication_type,
                    "reason": result.get("reason"),
                    "signal_id": result.get("signal_id"),
                    "aia_forward_attempted": bool(result.get("aia_forward_attempted")),
                    "aia_forward_ok": bool(result.get("aia_forward_ok")),
                    "aia_forward_error": result.get("aia_forward_error"),
                    "aia_forward_mode": result.get("aia_forward_mode"),
                    "aia_forward_warning": result.get("aia_forward_warning"),
                    "original_publication_type": publication_type,
                    **dima_window_audit,
                }
            )
            for key in (
                "duplicate_in_work_signal_detected",
                "duplicate_signal_id",
                "duplicate_signal_status",
                "publication_type",
                "replacement_selected",
                "replacement_reason",
                "related_signal_id",
                "refresh_of_signal_id",
                "replaces_signal_id",
                "previous_entry",
                "new_entry",
                "previous_lifecycle_state",
                "pending_update_classification",
                "replaced_signal_id",
                "replacing_signal_id",
                "old_entry",
                "new_candidate_entry",
                "old_sl",
                "new_candidate_sl",
                "entry_distance_pct",
                "sl_distance_pct",
                "rr_old",
                "rr_new",
                "confidence_old",
                "confidence_new",
                "strategy_same_or_compatible",
                "headline_risk_delta",
                "previous_risk_level",
                "new_risk_level",
                "risk_transition_reason",
                "duplicate_decision",
                "headline_update_generated",
                "confirmation_required",
                "confirmation_required_reason",
                "management_review_required",
                "management_review_reason",
                "risk_reassessment_required",
                "risk_reassessment_reason",
                "stale_active_signal_ignored",
                "stale_active_signal_id",
                "stale_active_signal_status",
                "stale_active_signal_age_hours",
                "stale_active_signal_reason",
                "stale_tp1_hit_signal_ignored",
                "stale_tp1_hit_signal_id",
                "stale_tp1_hit_signal_age_hours",
                "stale_tp1_hit_signal_reason",
                "scheduled_state_guard_duplicate_enforcement_enabled",
                "scheduled_state_guard_duplicate_runtime_eligible",
                "scheduled_state_guard_duplicate_runtime_action",
                "scheduled_state_guard_enforcement_action",
                "day_bias_direction",
                "candidate_direction",
                "signal_direction_vs_day_bias",
                "counter_regime_signal",
                "counter_regime_allowed_reason",
                "market_override_detected",
                "invalid_tp_ladder",
                "duplicate_tp_targets",
                "tp_ladder_warnings",
                "post_generation_event_risk_gate",
                "explicit_regime_flip_reason",
                "blocked_reason",
            ):
                row[key] = result.get(key)
            for key in STATE_GUARD_DECISION_LOG_FIELDS:
                row[key] = result.get(key)
        else:
            reason = str(result.get("reason") or "no_valid_signal_candidate")
            if attempt < cfg.max_attempts and reason in {"signal_core_no_trade", "no_valid_signal_candidate"}:
                retry_at = slot_time + timedelta(minutes=cfg.retry_delay_minutes)
                slots[slot_id] = {
                    "slot_id": slot_id,
                    "slot_time_msk": slot_time.isoformat(),
                    "attempt": attempt,
                    "status": "deferred",
                    "next_retry_at_msk": retry_at.isoformat(),
                    "reason": reason,
                    "selected_mode": gate["selected_mode"],
                    "signal_id": None,
                }
                row.update({"decision": "defer", "reason": reason})
            else:
                slots[slot_id] = {
                    "slot_id": slot_id,
                    "slot_time_msk": slot_time.isoformat(),
                    "attempt": attempt,
                    "status": "cancelled",
                    "next_retry_at_msk": None,
                    "reason": reason,
                    "selected_mode": gate["selected_mode"],
                    "signal_id": None,
                }
                row.update({"decision": "cancel", "reason": reason})
            for key in STATE_GUARD_DECISION_LOG_FIELDS:
                if key in result:
                    row[key] = result.get(key)
            for key in (
                "post_generation_event_risk_gate",
                "candidate_direction",
                "event_bias",
                "event_risk_level",
                "explicit_regime_flip_reason",
                "blocked_reason",
                "publication_type",
                "can_publish_full_signal",
                "execution_mode",
                "risk_size_mode",
                "entry_mode_required",
                "regime_confirmation_reasons",
                "candidate_confirm_only",
                "immediate_shock_window",
                "background_risk_active",
                "immediate_shock_until",
                "material_event_at",
                "last_material_change_at",
                "snapshot_built_at",
                "post_headline_reaction_observed",
                "structure_invalidated",
                "tactical_counter_risk_allowed",
                "tactical_counter_risk_failed_reasons",
                "conditions_to_eligibility",
                "confirmation_ttl_minutes",
            ):
                if key in result:
                    row[key] = result.get(key)
    except Exception as exc:
        slots[slot_id] = {
            "slot_id": slot_id,
            "slot_time_msk": slot_time.isoformat(),
            "attempt": attempt,
            "status": "error",
            "next_retry_at_msk": None,
            "reason": "system_routing_api_error",
            "selected_mode": gate["selected_mode"],
            "signal_id": None,
        }
        row.update({"decision": "error", "reason": "system_routing_api_error", "error": str(exc)})
    if not dry_run:
        write_json_atomic(cfg.signal_state_path, state)
        add_scheduled_decision_observability(row, cfg, result if "result" in locals() else None)
        append_signal_decision_with_health(now_utc, row)
        if "state_guard_shadow_available" in locals() and state_guard_shadow_available:
            append_jsonl(scheduled_state_guard_shadow_log_path(now_utc), state_guard_shadow_log_row(now_utc, slot_id, result))


async def run_publish_job(kind: str, now_utc: datetime, cfg: SchedulerConfig, *, dry_run: bool = False) -> None:
    state = read_json(cfg.publish_state_path)
    state.setdefault("day", {})
    state.setdefault("mid", {})
    now_msk = to_msk(now_utc)
    scheduled = slot_datetime_msk(now_msk.date(), cfg.day_time_msk if kind == "day" else cfg.mid_time_msk)
    key = scheduled.strftime("%Y%m%d") if kind == "day" else mid_cycle_id(now_msk.date(), cfg.start_date_msk, cfg.mid_interval_days)
    report_targets = scheduled_report_targets(kind, cfg)
    previous_state = state[kind].get(key, {}) if isinstance(state[kind].get(key), dict) else {}
    successful_targets = list(dict.fromkeys(previous_state.get("target_chat_ids") or []))
    pending_targets = [target for target in report_targets if target not in set(successful_targets)]
    row = {
        "ts_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "job_type": kind,
        "scheduled_date_msk": now_msk.date().isoformat(),
        "scheduled_time_msk": (cfg.day_time_msk if kind == "day" else cfg.mid_time_msk),
        "slot_time_msk": scheduled.isoformat(),
        "interval_days": cfg.mid_interval_days if kind == "mid" else None,
        "cycle_id": key if kind == "mid" else None,
        "decision": None,
        "target_chat_ids": report_targets,
        "pending_target_chat_ids": pending_targets,
        "artifact_path": None,
        "message_id": None,
        "signal_id": None,
        "error": None,
    }
    if previous_state.get("status") == "published" and not pending_targets:
        row["decision"] = "duplicate_skip"
        row["artifact_path"] = state[kind][key].get("artifact_path")
        row["message_id"] = state[kind][key].get("message_id")
        if not dry_run:
            append_jsonl(publish_decision_log_path(now_utc), row)
        return
    try:
        existing_artifact = previous_state.get("artifact_path")
        existing_report_dir = PROJECT_ROOT / existing_artifact if isinstance(existing_artifact, str) and existing_artifact else None
        result = await publish_report(
            kind,
            cfg,
            dry_run=dry_run,
            target_chat_ids=pending_targets,
            report_dir=existing_report_dir if existing_report_dir is not None and existing_report_dir.exists() else None,
        )
        delivered_targets = list(dict.fromkeys([*successful_targets, *result.get("target_chat_ids", [])]))
        remaining_targets = [target for target in report_targets if target not in set(delivered_targets)]
        state[kind][key] = {
            "status": "published" if not remaining_targets else "partial",
            "slot_time_msk": scheduled.isoformat(),
            "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
            "artifact_path": result.get("artifact_path"),
            "message_id": result.get("message_id"),
            "target_chat_ids": delivered_targets,
            "target_errors": result.get("target_errors") or {},
        }
        row.update(
            {
                "decision": "publish" if not remaining_targets else "partial",
                "artifact_path": result.get("artifact_path"),
                "message_id": result.get("message_id"),
                "successful_target_chat_ids": delivered_targets,
                "remaining_target_chat_ids": remaining_targets,
                "target_errors": result.get("target_errors") or {},
            }
        )
    except Exception as exc:
        row.update({"decision": "error", "error": str(exc)})
    if not dry_run:
        write_json_atomic(cfg.publish_state_path, state)
        append_jsonl(publish_decision_log_path(now_utc), row)


async def run_due_jobs(now_utc: datetime | None = None, *, job: str = "all", dry_run: bool = False) -> None:
    now_utc = now_utc or utc_now()
    cfg = SchedulerConfig.from_env()

    if job in {"all", "day"}:
        due, _ = day_due(now_utc, cfg)
        if not cfg.day_enabled:
            if not dry_run:
                append_jsonl(publish_decision_log_path(now_utc), {"ts_utc": now_utc.isoformat().replace("+00:00", "Z"), "job_type": "day", "decision": "skipped_disabled", "target_chat_ids": cfg.target_chat_ids, "error": None})
        elif due:
            await run_publish_job("day", now_utc, cfg, dry_run=dry_run)

    if job in {"all", "mid"}:
        due, _, _ = mid_due(now_utc, cfg)
        if not cfg.mid_enabled:
            if not dry_run:
                append_jsonl(publish_decision_log_path(now_utc), {"ts_utc": now_utc.isoformat().replace("+00:00", "Z"), "job_type": "mid", "decision": "skipped_disabled", "target_chat_ids": cfg.target_chat_ids, "error": None})
        elif due:
            await run_publish_job("mid", now_utc, cfg, dry_run=dry_run)

    if job in {"all", "signal"}:
        state = read_json(cfg.signal_state_path)
        state.setdefault("slots", {})
        due_slots = due_signal_slots(now_utc, cfg, state)
        if not cfg.signal_enabled:
            for slot_id, slot_time, attempt in due_slots:
                gate = evaluate_aia_gate(load_aia_context(), cfg)
                row = base_signal_log_row(now_utc, cfg, slot_id, slot_time, attempt, gate)
                row.update({"decision": "skipped_disabled", "reason": "scheduled_signal_disabled"})
                if not dry_run:
                    add_scheduled_decision_observability(row, cfg, stage=FUNNEL_STAGE_RUN_CANCELLED)
                    append_signal_decision_with_health(now_utc, row)
        else:
            for slot_id, slot_time, attempt in due_slots:
                await run_signal_slot(now_utc, cfg, state, slot_id, slot_time, attempt, dry_run=dry_run)


def parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", choices=["all", "day", "mid", "signal"], default="all")
    parser.add_argument("--now", help="Override current time, ISO-8601 with timezone")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_due_jobs(parse_now(args.now), job=args.job, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
