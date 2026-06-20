#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from macro_event_guard import evaluate_macro_event_guard, macro_dedupe_key, normalize_scheduled_macro_events

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGGER = logging.getLogger(__name__)
AGENT_STATE_REPO_ROOT = Path(os.getenv("STATE_GUARD_AGENT_REPO_ROOT", "/root/llm-signal-ai-agent"))
DEFAULT_STATE_GUARD_STATE_PATH = AGENT_STATE_REPO_ROOT / "logs/agent_trade_state.json"

DEFAULT_TARGET_CHAT_IDS = [-1003492385200, -1003493070625, -1003530482991]
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
    "state_guard_can_publish_full_signal",
    "state_guard_recommended_publication_type",
    "state_guard_reason",
    "state_guard_primary_signal_id",
    "state_guard_primary_lifecycle_state",
    "state_guard_primary_position_status",
    "state_guard_active_same_direction_scenario_found",
    "state_guard_active_same_direction_signal_id",
    "state_guard_active_same_direction_status",
    "state_guard_duplicate_detected",
    "state_guard_conflict_detected",
    "state_guard_replacement_candidate",
    "state_guard_entry_distance_pct",
    "state_guard_sl_distance_pct",
    "state_guard_secondary_signal_ids",
    "state_guard_explanation",
    "active_same_direction_scenario_found",
    "active_same_direction_signal_id",
    "active_same_direction_status",
    "duplicate_detected",
    "duplicate_enforcement_enabled",
    "duplicate_enforcement_action",
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
    return max(1, parse_int_env("SCHEDULED_WAIT_CONFIRM_FALLBACK_MAX_AGE_HOURS", 2))


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
        "confidence": payload.get("confidence"),
        "confidence_rank": _confidence_rank(payload.get("confidence")),
        "mode": str(payload.get("mode") or meta.get("mode") or "").strip().lower(),
        "holding_horizon": str(payload.get("holding_horizon") or "").strip().lower(),
        "strategy_type": str(payload.get("entry_mode") or meta.get("entry_type") or "").strip().lower(),
        "ema20_m15": _try_float(payload.get("ema20_m15")),
        "hard_block_conditions": payload.get("hard_block_conditions") if isinstance(payload.get("hard_block_conditions"), list) else [],
        "no_trade": bool(payload.get("no_trade")),
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
    return not (old_mode and new_mode and old_mode != new_mode)


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
        out.update(
            {
                "duplicate_in_work_signal_detected": True,
                "duplicate_signal_id": opposite.get("signal_id"),
                "duplicate_signal_status": opposite.get("status"),
                "publication_type": "conflict_update",
                "replacement_reason": "opposite_direction_in_work_signal",
                "duplicate_signal": opposite,
            }
        )
        return out

    entry_threshold = _try_float(os.getenv("SCHEDULED_DUPLICATE_ENTRY_DISTANCE_PCT")) or 0.5
    sl_threshold = _try_float(os.getenv("SCHEDULED_DUPLICATE_SL_DISTANCE_PCT")) or 1.0
    related: list[tuple[float, dict, float | None, float | None, bool]] = []
    for old in same_symbol:
        if old.get("direction") != direction:
            continue
        entry_distance = _distance_pct(old.get("entry_price"), candidate.get("entry_price"))
        sl_distance = _distance_pct(old.get("sl"), candidate.get("sl"))
        strategy_ok = _strategy_compatible(old, candidate)
        horizon_ok = _horizon_compatible(old, candidate)
        if (
            entry_distance is not None
            and entry_distance <= entry_threshold
            and (sl_distance is None or sl_distance <= sl_threshold)
            and strategy_ok
            and horizon_ok
        ):
            related.append((entry_distance, old, entry_distance, sl_distance, strategy_ok))

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
        return out
    if status not in WAIT_REPLACEABLE_STATUSES:
        out["publication_type"] = "active_signal_update"
        out["replacement_reason"] = "existing_signal_confirmed_or_armed"
        return out
    if old.get("filled"):
        out["replacement_reason"] = "old_signal_already_filled"
        return out
    if candidate.get("no_trade") or candidate.get("hard_block_conditions"):
        out["replacement_reason"] = "candidate_has_hard_block"
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
    return out


def _msk_label_from_signal_id(signal_id: str | None) -> str:
    text = str(signal_id or "")
    try:
        if len(text) >= 13 and text[8] == "_":
            return f"{text[9:11]}:{text[11:13]} МСК"
    except Exception:
        pass
    return "ранее"


def render_duplicate_update_message(decision: dict, candidate: dict) -> str:
    old = decision.get("duplicate_signal") if isinstance(decision.get("duplicate_signal"), dict) else {}
    symbol = candidate.get("display_symbol") or old.get("display_symbol") or _display_symbol(candidate.get("symbol"))
    side = str(candidate.get("direction") or old.get("direction") or "").upper()
    status = old.get("status") or decision.get("duplicate_signal_status") or "UNKNOWN"
    old_id = old.get("signal_id") or decision.get("duplicate_signal_id")
    if decision.get("publication_type") == "conflict_update":
        return (
            "🔄 RE_EVAL_ACTIVE_SIGNAL\n\n"
            f"{symbol} уже в работе, но новый scheduled scan видит противоположный bias.\n"
            f"Активный сигнал: {old_id} от {_msk_label_from_signal_id(old_id)}.\n"
            f"Статус AIA: {status}.\n\n"
            "Решение:\n"
            "Новый противоположный сигнал не публикуем автоматически.\n"
            "Нужна AIA management decision: HOLD / REDUCE / CLOSE / TRAIL / RE_EVAL.\n\n"
            "Это не новый вход."
        )
    title = "🔄 MANAGEMENT_UPDATE" if str(status).upper() in LIVE_POSITION_STATUSES else "🔄 ACTIVE SIGNAL UPDATE"
    entry_line = "Вход уже активирован." if str(status).upper() in LIVE_POSITION_STATUSES else "Вход ещё не активирован."
    return (
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
        "Это не новый сигнал."
    )


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
    for channel in cfg.target_chat_ids:
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
        )


def publish_decision_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_publish_decisions_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def signal_decision_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_signal_decisions_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def scheduled_state_guard_shadow_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_state_guard_shadow_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def _state_guard_base(enabled: bool, status: str) -> dict:
    return {
        "state_guard_shadow_enabled": enabled,
        "state_guard_status": status,
        "state_guard_decision": None,
        "state_guard_can_publish_full_signal": None,
        "state_guard_recommended_publication_type": None,
        "state_guard_reason": None,
        "state_guard_primary_signal_id": None,
        "state_guard_primary_lifecycle_state": None,
        "state_guard_primary_position_status": None,
        "state_guard_duplicate_detected": None,
        "state_guard_conflict_detected": None,
        "state_guard_replacement_candidate": None,
        "state_guard_entry_distance_pct": None,
        "state_guard_sl_distance_pct": None,
        "state_guard_secondary_signal_ids": [],
        "state_guard_explanation": None,
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
            "state_guard_can_publish_full_signal": evaluation.get("can_publish_full_signal"),
            "state_guard_recommended_publication_type": evaluation.get("recommended_publication_type"),
            "state_guard_reason": evaluation.get("reason"),
            "state_guard_primary_signal_id": evaluation.get("primary_signal_id"),
            "state_guard_primary_lifecycle_state": evaluation.get("primary_lifecycle_state"),
            "state_guard_primary_position_status": evaluation.get("primary_position_status"),
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
            "active_same_direction_scenario_found": evaluation.get("active_same_direction_scenario_found"),
            "active_same_direction_signal_id": evaluation.get("active_same_direction_signal_id")
            or evaluation.get("primary_signal_id"),
            "active_same_direction_status": evaluation.get("active_same_direction_status")
            or evaluation.get("primary_lifecycle_state"),
            "duplicate_detected": evaluation.get("duplicate_detected"),
        }
    )
    return out


def state_guard_duplicate_runtime_action(state_guard: dict, cfg: SchedulerConfig) -> dict:
    duplicate_decision = str(state_guard.get("state_guard_decision") or "").strip().upper()
    recommended = str(state_guard.get("state_guard_recommended_publication_type") or "").strip().upper()
    duplicate_detected = bool(state_guard.get("state_guard_duplicate_detected"))
    can_publish = state_guard.get("state_guard_can_publish_full_signal")
    active_status = str(state_guard.get("state_guard_primary_lifecycle_state") or "").strip().upper()
    entry_distance = _try_float(state_guard.get("state_guard_entry_distance_pct"))
    sl_distance = _try_float(state_guard.get("state_guard_sl_distance_pct"))
    eligible = (
        duplicate_detected
        and can_publish is False
        and active_status in ACTIVE_SAME_DIRECTION_DUPLICATE_BLOCKING_STATUSES
        and (entry_distance is None or entry_distance <= 0.5)
        and (sl_distance is None or sl_distance <= 1.0)
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
    allowed_reason = None
    if counter:
        allowed_reason = (
            payload.get("counter_regime_allowed_reason")
            or payload.get("market_override_reason")
            or ("market_override_detected" if market_override else "none")
        )
    return {
        "day_bias_direction": day_bias_direction or "unknown",
        "candidate_direction": candidate_direction or "unknown",
        "signal_direction_vs_day_bias": vs,
        "counter_regime_signal": counter,
        "counter_regime_allowed_reason": allowed_reason,
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
        "duplicate_detected": result.get("state_guard_duplicate_detected"),
        "conflict_detected": result.get("state_guard_conflict_detected"),
        "replacement_candidate": result.get("state_guard_replacement_candidate"),
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
        if isinstance(sstate, dict) and sstate.get("status") in {"published", "macro_substitution", "cancelled", "deferred", "error"}:
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
    for channel in cfg.target_chat_ids:
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


def _norm(value, default="unknown") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _candidate_from_context(ctx: dict) -> tuple[str, str]:
    candidate = ctx.get("signal_candidate")
    asset = None
    direction = None
    if isinstance(candidate, dict):
        asset = candidate.get("asset") or candidate.get("symbol")
        direction = candidate.get("direction") or candidate.get("side")
    asset = asset or ctx.get("focus_asset") or "none"
    direction = direction or ctx.get("focus_direction") or "unknown"
    return _norm(asset, "none").upper().split("/", 1)[0], _norm(direction).upper()


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


def evaluate_aia_gate(ctx: dict, cfg: SchedulerConfig) -> dict:
    status = _norm(ctx.get("status"), "unknown").upper()
    preferred_mode = _norm(ctx.get("preferred_mode"), "unknown").lower()
    event_risk_level = _norm(ctx.get("event_risk_level"), "unknown").lower()
    event_bias = _norm(ctx.get("event_bias"), "unknown").lower()
    confirm_policy = _norm(ctx.get("confirm_policy"), "unknown").lower()
    asset, direction = _candidate_from_context(ctx)
    reasons: list[str] = []

    if cfg.gate_mode == "strict" and status == "AVOID":
        reasons.append("aia_status_avoid")

    candidate_conflict = direction_conflicts_event_bias(direction, event_bias)
    if event_risk_level == "severe" and confirm_policy == "block_stale_confirm" and candidate_conflict:
        reasons.append("severe_block_stale_confirm_conflicts_event_bias")

    dominant = ctx.get("dominant_critical_topic")
    critical_topics = ctx.get("critical_topics")
    critical_active = bool(dominant) or bool(critical_topics)
    if critical_active and candidate_conflict:
        reasons.append("critical_topic_conflicts_event_bias")

    if event_bias == "risk_off" and is_risk_on_alt_long(asset, direction):
        reasons.append("risk_off_alt_long_without_reset_reclaim")

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
            selected_mode = "neutral"
            selected_mode_source = "aia_preferred_mode_soft_downgrade"
            preferred_mode_ignored_reason = ""
            soft_avoid_downgrade = True
            reason = "avoid_without_hard_block_downgrade_preferred_mode"
    elif preferred_mode in {"neutral", "conservative"} and cfg.signal_default_mode == "aggressive":
        selected_mode = "neutral"
        selected_mode_source = "aia_preferred_mode"

    return {
        "allowed": not hard_block_reasons,
        "reason": reason if not hard_block_reasons else ",".join(hard_block_reasons),
        "selected_mode": selected_mode if selected_mode in {"aggressive", "neutral"} else "aggressive",
        "selected_mode_source": selected_mode_source,
        "preferred_mode_downgrade_enabled": preferred_mode_downgrade_enabled,
        "preferred_mode_ignored_reason": preferred_mode_ignored_reason,
        "aia_avoid_soft_allowed": aia_avoid_soft_allowed,
        "soft_avoid_downgrade": soft_avoid_downgrade,
        "hard_block_reasons": hard_block_reasons,
        "aia_status": status,
        "preferred_mode": preferred_mode if preferred_mode in {"aggressive", "neutral", "conservative"} else "unknown",
        "focus_asset": asset,
        "focus_direction": direction,
        "flow_bias": _norm(ctx.get("flow_bias"), "unknown").lower(),
        "event_risk_level": event_risk_level,
        "dominant_critical_topic": dominant.get("topic_id") if isinstance(dominant, dict) else _norm(dominant, ""),
        "event_bias": event_bias,
        "confirm_policy": confirm_policy,
        "aia_context_missing": bool(ctx.get("aia_context_missing")),
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


async def publish_report(kind: str, cfg: SchedulerConfig, *, dry_run: bool = False) -> dict:
    import tg_bot

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
    for channel in cfg.target_chat_ids:
        before = len(context.bot.sent) if dry_run else 0
        await tg_bot._post_report(kind, emoji, context, channel, report_dir=report_dir)
        if dry_run:
            message_ids.extend(item["message_id"] for item in context.bot.sent[before:])
    return {"artifact_path": relpath(report_dir), "message_id": message_ids[0] if message_ids else None}


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
) -> dict:
    import tg_bot

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
    state_guard_shadow = evaluate_state_guard_shadow(candidate, cfg)
    state_guard_runtime = state_guard_duplicate_runtime_action(state_guard_shadow, cfg)
    day_diagnostics = day_bias_diagnostics(payload, candidate, gate={})
    if state_guard_runtime.get("scheduled_state_guard_duplicate_runtime_action") == "enforce_duplicate_suppression":
        decision = {
            "publication_type": str(
                state_guard_shadow.get("state_guard_recommended_publication_type") or "active_signal_update"
            ).strip().lower(),
            "duplicate_signal_id": state_guard_shadow.get("state_guard_primary_signal_id"),
            "duplicate_signal_status": state_guard_shadow.get("state_guard_primary_lifecycle_state"),
            "replacement_reason": state_guard_shadow.get("state_guard_reason") or "state_guard_duplicate_active_scenario",
            "duplicate_signal": {
                "signal_id": state_guard_shadow.get("state_guard_primary_signal_id"),
                "status": state_guard_shadow.get("state_guard_primary_lifecycle_state"),
                "display_symbol": candidate.get("display_symbol"),
                "direction": candidate.get("direction"),
            },
        }
        publication_type = decision["publication_type"]
        if publication_type not in {"active_signal_update", "suppress_duplicate"}:
            publication_type = "active_signal_update"
            decision["publication_type"] = publication_type
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
            "aia_forward_attempted": False,
            "aia_forward_ok": False,
            "aia_forward_error": None,
            "aia_forward_mode": "skipped_state_guard_duplicate",
            "aia_forward_warning": None,
            **state_guard_shadow,
            **state_guard_runtime,
            **day_diagnostics,
        }
    duplicate_decision = evaluate_duplicate_publication(candidate, load_in_work_signal_state(now_utc), now_utc=now_utc)
    duplicate_result = {k: v for k, v in duplicate_decision.items() if k != "duplicate_signal"}

    if duplicate_decision.get("publication_type") in {"active_signal_update", "conflict_update"}:
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
            **state_guard_shadow,
            **state_guard_runtime,
            **day_diagnostics,
            **duplicate_result,
        }

    publish_text = parts[0]
    if duplicate_decision.get("publication_type") == "replace_wait_confirm":
        publish_text = render_replacement_prefix(duplicate_decision, candidate) + parts[0]

    old_get_targets = tg_bot.get_main_publication_targets
    old_get_chat = tg_bot.get_main_publication_chat_id
    old_get_mode = tg_bot.get_user_mode
    try:
        tg_bot.get_main_publication_targets = lambda uid: cfg.target_chat_ids.copy()
        tg_bot.get_main_publication_chat_id = lambda uid: cfg.target_chat_ids[0] if cfg.target_chat_ids else None
        tg_bot.get_user_mode = lambda uid: selected_mode
        context = make_context(dry_run=dry_run)
        ok = await tg_bot._publish_signal_result(
            context,
            SCHEDULER_UID,
            text=publish_text,
            target_chat_id=cfg.target_chat_ids[0],
            delivery_kind="main",
            source="scheduled_runner.py:generate_and_publish_signal",
            symbol_hint=None,
            sig_html=Path(sig_html),
            run_log=Path(run_log) if run_log else None,
            skip_aia_forward=True,
        )
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
        try:
            tg_bot._AIA_UID_CONTEXT = SCHEDULER_UID
        except Exception:
            pass
        signal_json_v1 = tg_bot._build_signal_json_v1(
            signal_id=signal_id,
            published_at=published_at,
            channel_id=cfg.target_chat_ids[0] if cfg.target_chat_ids else None,
            origin_chat_id=cfg.target_chat_ids[0] if cfg.target_chat_ids else None,
            publish_targets=cfg.target_chat_ids.copy(),
            symbol_hint=None,
            last_payload=payload,
            last_json_path=PROJECT_ROOT / "logs/last.json",
        )
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
        **state_guard_shadow,
        **state_guard_runtime,
        **day_diagnostics,
        **duplicate_result,
    }


def base_signal_log_row(now_utc: datetime, cfg: SchedulerConfig, slot_id: str, slot_time: datetime, attempt: int, gate: dict) -> dict:
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
        "event_risk_level": gate.get("event_risk_level", "unknown"),
        "dominant_critical_topic": gate.get("dominant_critical_topic", ""),
        "event_bias": gate.get("event_bias", "unknown"),
        "confirm_policy": gate.get("confirm_policy", "unknown"),
        "hard_block_reasons": gate.get("hard_block_reasons", []),
        "target_chat_ids": cfg.target_chat_ids,
        "signal_id": None,
        "error": None,
        "aia_context_missing": bool(gate.get("aia_context_missing")),
    }


async def run_signal_slot(now_utc: datetime, cfg: SchedulerConfig, state: dict, slot_id: str, slot_time: datetime, attempt: int, *, dry_run: bool = False) -> None:
    slots = state.setdefault("slots", {})
    current = slots.get(slot_id)
    if isinstance(current, dict) and current.get("status") in {"published", "macro_substitution", "active_signal_update", "replace_wait_confirm", "conflict_update"}:
        gate = evaluate_aia_gate(load_aia_context(), cfg)
        row = base_signal_log_row(now_utc, cfg, slot_id, slot_time, attempt, gate)
        row.update({"decision": "duplicate_skip", "reason": f"already_{current.get('status')}", "signal_id": current.get("signal_id")})
        if not dry_run:
            append_jsonl(signal_decision_log_path(now_utc), row)
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
        if should_send:
            message = render_macro_event_message(message_type, event, classification)
            message_ids = await publish_macro_event_message(cfg, message, dry_run=dry_run)
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
            }
        )
        if not dry_run:
            write_json_atomic(cfg.signal_state_path, state)
            append_jsonl(signal_decision_log_path(now_utc), row)
        return

    if not gate["allowed"]:
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
            append_jsonl(signal_decision_log_path(now_utc), row)
        return

    try:
        result = await generate_and_publish_signal(str(gate["selected_mode"]), cfg, dry_run=dry_run, now_utc=now_utc)
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
                }
            )
            for key in (
                "duplicate_in_work_signal_detected",
                "duplicate_signal_id",
                "duplicate_signal_status",
                "publication_type",
                "replacement_selected",
                "replacement_reason",
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
        append_jsonl(signal_decision_log_path(now_utc), row)
        if "state_guard_shadow_available" in locals() and state_guard_shadow_available:
            append_jsonl(scheduled_state_guard_shadow_log_path(now_utc), state_guard_shadow_log_row(now_utc, slot_id, result))


async def run_publish_job(kind: str, now_utc: datetime, cfg: SchedulerConfig, *, dry_run: bool = False) -> None:
    state = read_json(cfg.publish_state_path)
    state.setdefault("day", {})
    state.setdefault("mid", {})
    now_msk = to_msk(now_utc)
    scheduled = slot_datetime_msk(now_msk.date(), cfg.day_time_msk if kind == "day" else cfg.mid_time_msk)
    key = scheduled.strftime("%Y%m%d") if kind == "day" else mid_cycle_id(now_msk.date(), cfg.start_date_msk, cfg.mid_interval_days)
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
        "target_chat_ids": cfg.target_chat_ids,
        "artifact_path": None,
        "message_id": None,
        "signal_id": None,
        "error": None,
    }
    if state[kind].get(key, {}).get("status") == "published":
        row["decision"] = "duplicate_skip"
        row["artifact_path"] = state[kind][key].get("artifact_path")
        row["message_id"] = state[kind][key].get("message_id")
        if not dry_run:
            append_jsonl(publish_decision_log_path(now_utc), row)
        return
    try:
        result = await publish_report(kind, cfg, dry_run=dry_run)
        state[kind][key] = {
            "status": "published",
            "slot_time_msk": scheduled.isoformat(),
            "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
            "artifact_path": result.get("artifact_path"),
            "message_id": result.get("message_id"),
            "target_chat_ids": cfg.target_chat_ids,
        }
        row.update({"decision": "publish", "artifact_path": result.get("artifact_path"), "message_id": result.get("message_id")})
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
                    append_jsonl(signal_decision_log_path(now_utc), row)
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
