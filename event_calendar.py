from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DEFAULT_EVENT_CALENDAR_PATH = BASE / "data" / "event_calendar.json"
MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

_VALID_CATEGORIES = {"macro", "fed", "politics", "crypto"}
_VALID_IMPACTS = {"high", "medium", "low"}
_DEFAULT_WINDOWS = {
    "high": (90, 120),
    "medium": (60, 90),
    "low": (30, 60),
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if not num.is_integer():
        return num
    return int(num)


def _normalize_impact(value) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    if text in {"very_high", "veryhigh", "critical", "severe"}:
        return "high"
    if text in {"med", "moderate"}:
        return "medium"
    if text in {"minor"}:
        return "low"
    return text if text in _VALID_IMPACTS else ""


def _normalize_category(value) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "central_bank": "fed",
        "fomc": "fed",
        "government": "politics",
        "regulation": "politics",
        "policy": "politics",
        "economy": "macro",
        "economic": "macro",
        "rates": "macro",
        "bitcoin": "crypto",
    }
    text = aliases.get(text, text)
    return text if text in _VALID_CATEGORIES else ""


def _parse_msk_date(value) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    cleaned = re.sub(r"\b(MSK|МСК)\b", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=MSK)
        except Exception:
            continue
    return None


def _parse_msk_datetime(value, *, date_hint=None) -> datetime | None:
    text = _text(value)
    if not text or re.search(r"\btbd\b", text, flags=re.IGNORECASE):
        return None

    cleaned = re.sub(r"\b(MSK|МСК)\b", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%d.%m.%Y, %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y, %H:%M", "%d.%m.%y %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=MSK)
        except Exception:
            continue

    match = re.search(r"(\d{1,2}):(\d{2})", cleaned)
    if not match:
        return None

    base_dt = _parse_msk_date(date_hint)
    if base_dt is None:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _normalize_generated_at_utc(value) -> str:
    text = _text(value)
    if not text:
        return utc_now_iso()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return utc_now_iso()


def _event_display_time(event: dict) -> str:
    time_text = _text(event.get("time_msk"))
    date_text = _text(event.get("date_msk"))
    if not time_text or re.search(r"\btbd\b", time_text, flags=re.IGNORECASE):
        return f"{date_text}, TBD" if date_text else "TBD"
    if re.search(r"\d{2}\.\d{2}\.\d{4}", time_text):
        return time_text
    if date_text:
        return f"{date_text}, {time_text}"
    return time_text


def _normalize_event(item) -> dict | None:
    if not isinstance(item, dict):
        return None

    event_name = _text(item.get("event"))
    if not event_name:
        return None

    impact = _normalize_impact(item.get("impact")) or "medium"
    category = _normalize_category(item.get("category")) or "macro"
    note = _text(item.get("note"))
    source = _text(item.get("source")) or "auto"

    time_text = _text(item.get("time_msk"))
    date_text = _text(item.get("date_msk"))

    exact_dt = _parse_msk_datetime(time_text, date_hint=date_text)
    if exact_dt is not None:
        date_text = exact_dt.strftime("%d.%m.%Y")
        time_text = exact_dt.strftime("%d.%m.%Y, %H:%M")
    else:
        parsed_date = _parse_msk_date(date_text)
        if parsed_date is not None:
            date_text = parsed_date.strftime("%d.%m.%Y")
        if not time_text or re.search(r"\btbd\b", time_text, flags=re.IGNORECASE):
            time_text = "TBD"

    before_default, after_default = _DEFAULT_WINDOWS.get(impact, _DEFAULT_WINDOWS["medium"])
    before = _number(item.get("window_before_min"))
    after = _number(item.get("window_after_min"))

    out = {
        "time_msk": time_text or "TBD",
        "event": event_name,
        "category": category,
        "impact": impact,
        "window_before_min": before if before is not None else before_default,
        "window_after_min": after if after is not None else after_default,
        "source": source,
        "note": note or "Auto-imported scheduled event.",
    }
    if date_text:
        out["date_msk"] = date_text
    return out


def normalize_event_calendar(raw) -> dict:
    generated_at_utc = ""
    raw_events = []
    if isinstance(raw, dict):
        generated_at_utc = _normalize_generated_at_utc(raw.get("generated_at_utc"))
        raw_events = raw.get("events") or []
    else:
        generated_at_utc = utc_now_iso()

    events: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_events if isinstance(raw_events, list) else []:
        normalized = _normalize_event(item)
        if normalized is None:
            continue
        fingerprint = (
            _text(normalized.get("event")).lower(),
            _text(normalized.get("date_msk")),
            _text(normalized.get("time_msk")),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        events.append(normalized)

    events.sort(
        key=lambda event: (
            1 if _parse_msk_datetime(event.get("time_msk"), date_hint=event.get("date_msk")) is None else 0,
            _parse_msk_datetime(event.get("time_msk"), date_hint=event.get("date_msk"))
            or _parse_msk_date(event.get("date_msk"))
            or datetime.max.replace(tzinfo=MSK),
            _text(event.get("event")).lower(),
        )
    )

    return {
        "generated_at_utc": generated_at_utc,
        "events": events,
    }


def read_event_calendar(path: Path | None = None) -> dict:
    calendar_path = path or DEFAULT_EVENT_CALENDAR_PATH
    try:
        raw = json.loads(calendar_path.read_text(encoding="utf-8"))
    except Exception:
        return normalize_event_calendar({"generated_at_utc": utc_now_iso(), "events": []})
    return normalize_event_calendar(raw)


def _coerce_now_msk(now_msk=None) -> datetime:
    if isinstance(now_msk, datetime):
        return now_msk.astimezone(MSK)
    parsed = _parse_msk_datetime(now_msk) if now_msk is not None else None
    return parsed or datetime.now(MSK)


def profile_horizon_hours(profile: str) -> int:
    name = _text(profile).lower()
    if name == "mid":
        return 24 * 7
    return 24


def select_calendar_events(
    calendar: dict | None = None,
    *,
    path: Path | None = None,
    now_msk=None,
    horizon_hours: int = 24,
    max_events: int | None = None,
) -> list[dict]:
    payload = normalize_event_calendar(calendar) if isinstance(calendar, dict) else read_event_calendar(path)
    now_dt = _coerce_now_msk(now_msk)
    cutoff = now_dt + timedelta(hours=max(int(horizon_hours), 0))

    selected: list[tuple[tuple[int, datetime, str], dict]] = []
    for item in payload.get("events") or []:
        event = copy.deepcopy(item)
        exact_dt = _parse_msk_datetime(event.get("time_msk"), date_hint=event.get("date_msk"))
        if exact_dt is not None:
            if now_dt <= exact_dt <= cutoff:
                selected.append(((0, exact_dt, _text(event.get("event")).lower()), event))
            continue

        date_dt = _parse_msk_date(event.get("date_msk"))
        if date_dt is None:
            continue
        if now_dt.date() <= date_dt.date() <= cutoff.date():
            selected.append(((1, date_dt, _text(event.get("event")).lower()), event))

    selected.sort(key=lambda item: item[0])
    events = [copy.deepcopy(event) for _, event in selected]
    if max_events is not None and max_events >= 0:
        return events[:max_events]
    return events


def build_calendar_context(
    profile: str,
    *,
    path: Path | None = None,
    now_msk=None,
    max_events: int | None = None,
) -> dict:
    calendar = read_event_calendar(path)
    return {
        "generated_at_utc": calendar.get("generated_at_utc"),
        "calendar_events": select_calendar_events(
            calendar,
            now_msk=now_msk,
            horizon_hours=profile_horizon_hours(profile),
            max_events=max_events,
        ),
    }


def build_calendar_risk_summary(events: list[dict]) -> str:
    if not isinstance(events, list) or not events:
        return ""

    fragments: list[str] = []
    primary = events[:3]
    for item in primary:
        label = _text(item.get("event")) or "Scheduled event"
        impact = _normalize_impact(item.get("impact")) or "medium"
        fragments.append(f"{_event_display_time(item)} — {label} ({impact})")

    high_by_date: dict[str, int] = {}
    for item in events:
        if _normalize_impact(item.get("impact")) != "high":
            continue
        date_key = _text(item.get("date_msk"))
        if date_key:
            high_by_date[date_key] = high_by_date.get(date_key, 0) + 1
    clustered_dates = [date_key for date_key, count in high_by_date.items() if count >= 2]
    if clustered_dates:
        fragments.append(f"clustered scheduled risk on {clustered_dates[0]}")

    return "; ".join(fragments[:3])


def render_calendar_section(
    events: list[dict],
    *,
    title: str = "🗓 Ключевые события периода",
    max_items: int = 5,
    empty_message: str = "Нет подтвержденных scheduled events в event calendar.",
) -> str:
    lines = [title]
    if not isinstance(events, list) or not events:
        lines.append(f"- {empty_message}")
        return "\n".join(lines)

    for item in events[: max(max_items, 0)]:
        label = _text(item.get("event")) or "Scheduled event"
        impact = _normalize_impact(item.get("impact")) or "medium"
        lines.append(f"- {_event_display_time(item)} | {label} | impact: {impact}")
    return "\n".join(lines)
