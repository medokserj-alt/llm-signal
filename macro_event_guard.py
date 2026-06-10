from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

HIGH_IMPACT_NEEDLES = (
    "cpi",
    "core cpi",
    "ppi",
    "nfp",
    "nonfarm",
    "payroll",
    "fomc",
    "rate decision",
    "fed press conference",
    "powell",
)
FORCED_HIGH_IMPACT_NEEDLES = (
    "cpi",
    "core cpi",
    "ppi",
    "nfp",
    "nonfarm",
    "payroll",
    "fomc",
    "rate decision",
    "fed press conference",
)
VALID_CATEGORIES = {"macro_inflation", "macro_rates", "jobs", "fed", "other"}
DEFAULT_MACRO_POST_EVENT_ANALYSIS_TTL_MINUTES = 90
DEFAULT_MACRO_EVENT_GUARD_MAX_AGE_MINUTES = 180


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(float(os.getenv(name, str(default)))), 0)
    except Exception:
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _impact(value: Any, name: str) -> str:
    low_name = name.lower()
    if any(needle in low_name for needle in FORCED_HIGH_IMPACT_NEEDLES):
        return "high"
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    if text in {"critical", "severe", "very_high", "veryhigh"}:
        return "high"
    if text in {"medium", "med", "moderate"}:
        return "medium"
    if text in {"low", "minor"}:
        return "low"
    return "high" if any(needle in low_name for needle in HIGH_IMPACT_NEEDLES) else "medium"


def _category(value: Any, name: str) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    low_name = name.lower()
    if "cpi" in low_name or "ppi" in low_name or "pce" in low_name or "inflation" in low_name:
        return "macro_inflation"
    if "nfp" in low_name or "payroll" in low_name or "jobs" in low_name or "employment" in low_name:
        return "jobs"
    if "powell" in low_name or "fomc" in low_name or "fed" in low_name:
        return "fed"
    if text in {"macro_inflation", "macro_rates", "jobs", "fed", "other"}:
        return text
    if text in {"macro", "rates", "rate", "economic", "economy"}:
        return "macro_rates" if "rate" in low_name else "other"
    return "other"


def _parse_msk_date(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    cleaned = re.sub(r"\b(MSK|МСК)\b", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=MSK)
        except Exception:
            pass
    return None


def parse_msk_datetime(value: Any, *, date_hint: Any = None, fallback_dt: datetime | None = None) -> datetime | None:
    text = _text(value)
    if not text or re.search(r"\btbd\b", text, flags=re.IGNORECASE):
        return None
    cleaned = re.sub(r"\b(MSK|МСК)\b", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%d.%m.%Y, %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y, %H:%M", "%d.%m.%y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=MSK)
        except Exception:
            pass
    match = re.search(r"(\d{1,2}):(\d{2})", cleaned)
    if not match:
        return None
    base = _parse_msk_date(date_hint) if date_hint else None
    if base is None:
        base = fallback_dt.astimezone(MSK) if isinstance(fallback_dt, datetime) else None
    if base is None:
        return None
    return base.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _event_id(name: str, event_dt: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "macro_event"
    return f"{slug}_{event_dt.astimezone(MSK).strftime('%Y%m%d_%H%M')}"


def _load_state(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _classification_for(event: dict, state: dict) -> dict | None:
    own = event.get("post_event_classification")
    if isinstance(own, dict) and own:
        return own
    by_id = state.get("post_event_classifications") if isinstance(state.get("post_event_classifications"), dict) else {}
    event_id = event.get("event_id")
    if isinstance(event_id, str) and isinstance(by_id.get(event_id), dict):
        return by_id[event_id]
    events = state.get("events") if isinstance(state.get("events"), dict) else {}
    if isinstance(event_id, str) and isinstance(events.get(event_id), dict):
        nested = events[event_id].get("post_event_classification")
        if isinstance(nested, dict) and nested:
            return nested
    return None


def normalize_scheduled_macro_events(raw_events: Any, *, source: str = "calendar", now: datetime | None = None, state_path: Path | None = None) -> list[dict]:
    if isinstance(raw_events, dict):
        iterable = [raw_events]
    elif isinstance(raw_events, list):
        iterable = raw_events
    else:
        iterable = []
    fallback_dt = (now or datetime.now(UTC)).astimezone(MSK)
    state = _load_state(state_path)
    out: list[dict] = []
    seen: set[str] = set()
    for item in iterable:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("event_name") or item.get("event") or item.get("name"))
        if not name:
            continue
        event_dt = None
        if item.get("event_time_utc"):
            try:
                event_dt = datetime.fromisoformat(_text(item.get("event_time_utc")).replace("Z", "+00:00")).astimezone(MSK)
            except Exception:
                event_dt = None
        if event_dt is None:
            event_dt = parse_msk_datetime(item.get("event_time_msk") or item.get("time_msk"), date_hint=item.get("date_msk"), fallback_dt=fallback_dt)
        if event_dt is None:
            continue
        impact = _impact(item.get("impact"), name)
        if impact != "high" and not any(needle in name.lower() for needle in HIGH_IMPACT_NEEDLES):
            continue
        pre = item.get("pre_blackout_minutes", item.get("window_before_min"))
        try:
            pre_minutes = int(float(pre))
        except Exception:
            pre_minutes = 90
        normalized = {
            "event_id": _text(item.get("event_id")) or _event_id(name, event_dt),
            "event_name": name,
            "category": _category(item.get("category"), name),
            "impact": impact,
            "event_time_utc": _iso_z(event_dt),
            "event_time_msk": event_dt.strftime("%d.%m.%Y, %H:%M"),
            "pre_blackout_minutes": max(pre_minutes, 0),
            "post_analysis_required": bool(item.get("post_analysis_required", True)),
            "policy_pre_event": _text(item.get("policy_pre_event")) or "no_new_entries",
            "policy_at_event": _text(item.get("policy_at_event")) or "macro_event_update_required",
            "policy_post_event": _text(item.get("policy_post_event")) or "require_reprice_analysis",
            "substitute_scheduled_signal": bool(item.get("substitute_scheduled_signal", True)),
            "source": _text(item.get("source")) or source,
        }
        for key in ("actual", "forecast", "previous"):
            if item.get(key) not in (None, ""):
                normalized[key] = item.get(key)
        if isinstance(item.get("post_event_classification"), dict):
            normalized["post_event_classification"] = item["post_event_classification"]
        cls = _classification_for({**item, **normalized}, state)
        if isinstance(cls, dict) and cls:
            normalized["post_event_classification"] = cls
        if normalized["event_id"] in seen:
            continue
        seen.add(normalized["event_id"])
        out.append(normalized)
    out.sort(key=lambda event: event.get("event_time_utc") or "")
    return out


def evaluate_macro_event_guard(
    *,
    now: datetime,
    events: list[dict],
    side: str | None = None,
    forced_override: bool = False,
    state_path: Path | None = None,
) -> dict:
    now_utc = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    normalized = normalize_scheduled_macro_events(events, now=now_utc, state_path=state_path)
    post_event_ttl_minutes = _env_int("MACRO_POST_EVENT_ANALYSIS_TTL_MINUTES", DEFAULT_MACRO_POST_EVENT_ANALYSIS_TTL_MINUTES)
    guard_max_age_minutes = _env_int("MACRO_EVENT_GUARD_MAX_AGE_MINUTES", DEFAULT_MACRO_EVENT_GUARD_MAX_AGE_MINUTES)
    expired_guard: dict | None = None
    for event in normalized:
        try:
            event_dt = datetime.fromisoformat(str(event["event_time_utc"]).replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            continue
        pre_start = event_dt - timedelta(minutes=int(event.get("pre_blackout_minutes") or 90))
        cls = event.get("post_event_classification") if isinstance(event.get("post_event_classification"), dict) else None
        age_minutes = int((now_utc - event_dt).total_seconds() // 60) if now_utc >= event_dt else None
        if pre_start <= now_utc < event_dt:
            remaining = int(max((event_dt - now_utc).total_seconds() // 60, 0))
            return {
                "active": True,
                "blocked": not forced_override,
                "phase": "pre_event",
                "reason": "scheduled_macro_pre_event_blackout",
                "macro_policy": "no_new_entries",
                "macro_message_type": "PRE_EVENT_MACRO_NOTICE",
                "event": event,
                "blackout_remaining_minutes": remaining,
                "event_age_minutes": age_minutes,
                "post_event_classification": cls,
            }
        if (
            now_utc >= event_dt
            and event.get("post_analysis_required", True)
            and not cls
            and age_minutes is not None
            and age_minutes <= post_event_ttl_minutes
        ):
            return {
                "active": True,
                "blocked": not forced_override,
                "phase": "awaiting_reprice",
                "reason": "post_event_reprice_required",
                "macro_policy": "require_reprice_analysis",
                "macro_message_type": "MACRO_EVENT_ANALYSIS_PENDING",
                "event": event,
                "blackout_remaining_minutes": 0,
                "event_age_minutes": age_minutes,
                "post_event_classification": None,
            }
        if (
            now_utc >= event_dt
            and event.get("post_analysis_required", True)
            and not cls
            and age_minutes is not None
            and age_minutes > guard_max_age_minutes
        ):
            expired_guard = {
                "active": False,
                "blocked": False,
                "phase": "expired_missing_classification",
                "reason": "macro_event_reprice_expired_without_classification",
                "macro_policy": "expired_stale_event_ignored",
                "macro_message_type": None,
                "event": event,
                "blackout_remaining_minutes": 0,
                "event_age_minutes": age_minutes,
                "post_event_classification": None,
                "events": normalized,
            }
            continue
        if now_utc >= event_dt and cls:
            allowed = _text(cls.get("allowed_direction")).lower()
            policy = _text(cls.get("execution_policy")).lower()
            old_valid = cls.get("old_narrative_valid")
            blocked = False
            reason = "post_event_classified"
            side_l = _text(side).lower()
            if policy == "no_trade_chaotic" or allowed == "none":
                blocked = True
                reason = "post_event_reaction_chaotic"
            elif side_l == "short" and allowed == "long" and old_valid is False:
                blocked = True
                reason = "macro_event_direction_requires_fresh_breakdown"
            elif side_l == "long" and allowed == "short":
                blocked = True
                reason = "macro_event_direction_requires_fresh_reclaim"
            elif policy == "no_chase_wait_pullback":
                reason = "post_event_no_chase_wait_retest"
            return {
                "active": True,
                "blocked": blocked and not forced_override,
                "phase": "post_event_classified",
                "reason": reason,
                "macro_policy": policy or "trade_allowed_strict_confirm",
                "macro_message_type": "MACRO_NO_TRADE_RECOMMENDATION" if blocked else "MACRO_TRADE_PROPOSAL",
                "event": event,
                "blackout_remaining_minutes": 0,
                "event_age_minutes": age_minutes,
                "post_event_classification": cls,
            }
    if expired_guard:
        return expired_guard
    return {"active": False, "blocked": False, "phase": "none", "reason": "none", "events": normalized}


def macro_dedupe_key(event: dict, message_type: str, phase: str, now: datetime) -> str:
    if message_type == "PRE_EVENT_MACRO_NOTICE":
        bucket_minutes = 30
    elif message_type == "MACRO_EVENT_ANALYSIS_PENDING":
        return f"{event.get('event_id')}:{message_type}:{phase}"
    else:
        bucket_minutes = 24 * 60
    now_msk = now.astimezone(MSK)
    minute = (now_msk.hour * 60 + now_msk.minute) // bucket_minutes * bucket_minutes
    return f"{event.get('event_id')}:{message_type}:{phase}:{now_msk.strftime('%Y%m%d')}:{minute}"
