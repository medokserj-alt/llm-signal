#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_calendar import (  # noqa: E402
    DEFAULT_EVENT_CALENDAR_PATH,
    MSK,
    build_calendar_context,
    normalize_event_calendar,
    read_event_calendar,
    select_calendar_events,
    utc_now_iso,
)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
_DEFAULT_TIMEOUT = 20
_USER_AGENT = "llm-signal-next-event-calendar/2.0"

_FED_CALENDAR_INDEX_URL = "https://www.federalreserve.gov/newsevents/calendar.htm"
_FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_BEA_RELEASE_DATES_URL = "https://apps.bea.gov/API/signup/release_dates.json"
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_TO_NUMBER = {name: index for index, name in enumerate(_MONTH_NAMES, start=1)}
_FED_SECTION_NAMES = {
    "Speeches",
    "FOMC Meetings",
    "Beige Book",
    "Statistical Releases",
    "Other",
}
_IMPACT_WINDOWS = {
    "high": (90, 120),
    "medium": (60, 90),
    "low": (30, 60),
}
_IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}
_BEA_EVENT_CONFIG = {
    "Gross Domestic Product": {
        "event": "US GDP",
        "category": "macro",
        "impact": "high",
        "note": "BEA Gross Domestic Product release.",
        "priority": 90,
    },
    "Personal Income and Outlays": {
        "event": "US Personal Income and Outlays (PCE)",
        "category": "macro",
        "impact": "high",
        "note": "BEA Personal Income and Outlays release, including PCE inflation.",
        "priority": 90,
    },
}
_BLS_SOURCE_CONFIGS = (
    {
        "name": "bls_cpi",
        "url": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "event": "US CPI",
        "category": "macro",
        "impact": "high",
        "note": "BLS Consumer Price Index release.",
        "priority": 80,
    },
    {
        "name": "bls_ppi",
        "url": "https://www.bls.gov/schedule/news_release/ppi.htm",
        "event": "US PPI",
        "category": "macro",
        "impact": "medium",
        "note": "BLS Producer Price Index release.",
        "priority": 80,
    },
    {
        "name": "bls_employment",
        "url": "https://www.bls.gov/schedule/news_release/empsit.htm",
        "event": "US Employment Situation (NFP)",
        "category": "macro",
        "impact": "high",
        "note": "BLS Employment Situation release.",
        "priority": 80,
    },
)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _fetch_text_url(url: str, *, timeout: int = _DEFAULT_TIMEOUT) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _fetch_json_url(url: str, *, timeout: int = _DEFAULT_TIMEOUT):
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset, errors="replace")
    return json.loads(raw)


def _clean_html_fragment(fragment: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?is)</(p|div|li|tr|td|th|h\d|section|article|ul|ol)>", "\n", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned).replace("\xa0", " ")
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
    return cleaned.strip()


def _html_lines(raw_html: str) -> list[str]:
    lines: list[str] = []
    for line in _clean_html_fragment(raw_html).splitlines():
        cleaned = re.sub(r"\s+", " ", _text(line))
        if cleaned:
            lines.append(cleaned)
    return lines


def _extract_html_table_rows(raw_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_html in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", raw_html):
        cells = [
            re.sub(r"\s+", " ", _clean_html_fragment(cell))
            for cell in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html)
        ]
        cleaned_cells = [_text(cell) for cell in cells if _text(cell)]
        if cleaned_cells:
            rows.append(cleaned_cells)
    return rows


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        self._href = href
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = re.sub(r"\s+", " ", _text("".join(self._chunks)))
        if text:
            self.links.append((text, self._href))
        self._href = None
        self._chunks = []


def _normalize_month_text(value: str) -> str:
    cleaned = _text(value)
    cleaned = cleaned.replace("Sept.", "Sep.")
    cleaned = re.sub(r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_us_date(value: str) -> date | None:
    cleaned = _normalize_month_text(value)
    if not cleaned:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except Exception:
            continue
    return None


def _parse_time_text(value: str) -> time | None:
    cleaned = _text(value)
    if not cleaned:
        return None
    cleaned = cleaned.lower().replace("a.m.", "am").replace("p.m.", "pm")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if cleaned.endswith("am"):
        cleaned = f"{cleaned[:-2].strip()} AM"
    elif cleaned.endswith("pm"):
        cleaned = f"{cleaned[:-2].strip()} PM"
    cleaned = cleaned.upper()
    for fmt in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except Exception:
            continue
    return None


def _combine_local_datetime(event_date: date, time_text: str, *, tz: ZoneInfo = ET) -> datetime | None:
    parsed_time = _parse_time_text(time_text)
    if parsed_time is None:
        return None
    return datetime.combine(event_date, parsed_time, tzinfo=tz).astimezone(UTC)


def _looks_like_time(value: str) -> bool:
    return bool(re.match(r"^\d{1,2}(?::\d{2})?\s*(?:AM|PM|a\.m\.|p\.m\.)$", _text(value)))


def _looks_like_day_list(value: str) -> bool:
    return bool(re.match(r"^\d{1,2}(?:\s*,\s*\d{1,2})*$", _text(value)))


def _parse_day_values(value: str) -> list[int]:
    days: list[int] = []
    for token in re.split(r"\s*,\s*", _text(value)):
        if token.isdigit():
            days.append(int(token))
    return [day for day in days if 1 <= day <= 31]


def _default_windows(impact: str) -> tuple[int, int]:
    return _IMPACT_WINDOWS.get(_text(impact), _IMPACT_WINDOWS["medium"])


def _build_event(
    *,
    event: str,
    category: str,
    impact: str,
    note: str,
    dt_utc: datetime | None = None,
    date_local: date | None = None,
    source: str = "auto",
    window_before_min: int | None = None,
    window_after_min: int | None = None,
    identity: str | None = None,
    priority: int = 0,
) -> dict:
    before_default, after_default = _default_windows(impact)
    before = int(window_before_min if window_before_min is not None else before_default)
    after = int(window_after_min if window_after_min is not None else after_default)
    out = {
        "time_msk": "TBD",
        "event": _text(event),
        "category": _text(category) or "macro",
        "impact": _text(impact) or "medium",
        "window_before_min": before,
        "window_after_min": after,
        "source": _text(source) or "auto",
        "note": _text(note) or "Auto-imported scheduled event.",
        "_priority": priority,
        "_identity": _text(identity),
    }
    if dt_utc is not None:
        dt_msk = dt_utc.astimezone(MSK)
        out["time_msk"] = dt_msk.strftime("%d.%m.%Y, %H:%M")
        out["date_msk"] = dt_msk.strftime("%d.%m.%Y")
    elif date_local is not None:
        out["time_msk"] = "TBD"
        out["date_msk"] = date_local.strftime("%d.%m.%Y")
    return out


def _event_identity(event: dict) -> str:
    explicit = _text(event.get("_identity"))
    if explicit:
        return explicit
    event_name = re.sub(r"\s+", " ", _text(event.get("event")).lower())
    category = _text(event.get("category")).lower()
    date_msk = _text(event.get("date_msk"))
    return f"{category}:{event_name}:{date_msk}"


def _event_rank(event: dict) -> tuple[int, int, int, str]:
    has_exact_time = 0 if _text(event.get("time_msk")).upper() == "TBD" else 1
    priority = int(event.get("_priority") or 0)
    impact_rank = _IMPACT_RANK.get(_text(event.get("impact")).lower(), 0)
    note = _text(event.get("note"))
    return has_exact_time, priority, impact_rank, note


def _dedupe_events(events: list[dict]) -> list[dict]:
    selected: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        key = _event_identity(event)
        current = selected.get(key)
        if current is None or _event_rank(event) > _event_rank(current):
            selected[key] = event
    return list(selected.values())


def _default_sources(now_utc: datetime | None = None) -> list[dict]:
    _ = now_utc or datetime.now(UTC)
    return [
        {
            "name": "fed_fomc",
            "reader": _read_fed_fomc_events,
            "url": _FED_FOMC_URL,
        },
        {
            "name": "fed_calendar",
            "reader": _read_fed_calendar_events,
            "url": _FED_CALENDAR_INDEX_URL,
        },
        {
            "name": "bea_release_dates",
            "reader": _read_bea_release_dates_events,
            "url": _BEA_RELEASE_DATES_URL,
        },
        *[
            {
                **config,
                "reader": _read_bls_schedule_events,
            }
            for config in _BLS_SOURCE_CONFIGS
        ],
    ]


def _parse_bls_schedule_html(
    raw_html: str,
    *,
    event: str,
    category: str,
    impact: str,
    note: str,
    priority: int = 0,
) -> list[dict]:
    events: list[dict] = []
    for cells in _extract_html_table_rows(raw_html):
        if len(cells) < 3:
            continue
        if _text(cells[0]).lower().startswith("reference month"):
            continue
        reference_period = _text(cells[0])
        release_date = _parse_us_date(cells[-2])
        release_time = _text(cells[-1])
        if release_date is None:
            continue
        dt_utc = _combine_local_datetime(release_date, release_time, tz=ET)
        note_text = _text(note)
        if reference_period:
            note_text = f"{note_text} Reference period: {reference_period}."
        events.append(
            _build_event(
                event=event,
                category=category,
                impact=impact,
                note=note_text,
                dt_utc=dt_utc,
                date_local=release_date if dt_utc is None else None,
                identity=f"{_text(event).lower()}:{release_date.isoformat()}",
                priority=priority,
            )
        )
    return events


def _parse_bea_release_dates_json(payload) -> list[dict]:
    events: list[dict] = []
    if not isinstance(payload, dict):
        return events
    for series_name, config in _BEA_EVENT_CONFIG.items():
        entry = payload.get(series_name)
        if not isinstance(entry, dict):
            continue
        release_dates = entry.get("release_dates")
        if not isinstance(release_dates, list):
            continue
        for raw_dt in release_dates:
            text = _text(raw_dt)
            if not text:
                continue
            try:
                dt_utc = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
            except Exception:
                continue
            local_date = dt_utc.astimezone(ET).date()
            events.append(
                _build_event(
                    event=config["event"],
                    category=config["category"],
                    impact=config["impact"],
                    note=config["note"],
                    dt_utc=dt_utc,
                    identity=f"{config['event'].lower()}:{local_date.isoformat()}",
                    priority=int(config.get("priority") or 0),
                )
            )
    return events


def _parse_fed_fomc_calendar_html(raw_html: str) -> list[dict]:
    lines = _html_lines(raw_html)
    events: list[dict] = []
    current_year: int | None = None
    current_month: int | None = None

    for line in lines:
        year_match = re.match(r"^(\d{4}) FOMC Meetings$", line)
        if year_match:
            current_year = int(year_match.group(1))
            current_month = None
            continue
        if current_year is None:
            continue
        if line in _MONTH_TO_NUMBER:
            current_month = _MONTH_TO_NUMBER[line]
            continue
        if current_month is None:
            continue

        match = re.match(r"^(\d{1,2})(?:-(\d{1,2}))?\*?$", line.replace(" ", ""))
        if match is None:
            continue
        start_day = int(match.group(1))
        end_day = int(match.group(2) or match.group(1))
        try:
            meeting_date = date(current_year, current_month, end_day)
        except ValueError:
            continue
        if start_day != end_day:
            note = f"Two-day FOMC meeting, {_MONTH_NAMES[current_month - 1]} {start_day}-{end_day}."
        else:
            note = f"Scheduled FOMC meeting on {_MONTH_NAMES[current_month - 1]} {end_day}."
        events.append(
            _build_event(
                event="FOMC Meeting",
                category="fed",
                impact="high",
                note=note,
                date_local=meeting_date,
                identity=f"fomc_meeting:{meeting_date.isoformat()}",
                priority=40,
            )
        )
    return events


def _parse_fed_calendar_index_html(raw_html: str, *, base_url: str) -> dict[str, str]:
    parser = _AnchorCollector()
    parser.feed(raw_html)
    links: dict[str, str] = {}
    for text, href in parser.links:
        if re.match(r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{4}$", text):
            links[text] = urljoin(base_url, href)
    return links


def _normalize_fed_speech_event(title: str) -> tuple[str, str] | None:
    cleaned = _text(title)
    if not cleaned:
        return None
    _, _, speaker = cleaned.partition(" - ")
    speaker = speaker or cleaned
    if "Powell" in speaker:
        return "Jerome Powell speech", "high"
    if re.search(r"\b(Chair|Vice Chair|Vice Chair for Supervision|Governor)\b", speaker):
        return f"Fed speech - {speaker}", "medium"
    return None


def _fed_speech_note(details: list[str]) -> str:
    meaningful = [item for item in details if item not in {"Watch Live", "PDF", "HTML", "Video", "Audio"}]
    if meaningful:
        return " ".join(meaningful[:2])
    return "Scheduled Federal Reserve speaker event."


def _parse_fed_calendar_month_html(raw_html: str) -> list[dict]:
    lines = _html_lines(raw_html)
    header = next(
        (
            line
            for line in lines
            if re.match(
                r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{4}$",
                line,
            )
        ),
        "",
    )
    if not header:
        return []
    month_name, year_text = header.split(" ", 1)
    month_number = _MONTH_TO_NUMBER.get(month_name)
    if month_number is None:
        return []
    year_number = int(year_text)
    events: list[dict] = []
    section = ""
    index = 0

    while index < len(lines):
        line = lines[index]
        if line in _FED_SECTION_NAMES:
            section = line
            index += 1
            continue
        if line in {"Previous", "Next", "Time:", "Release Date(s):"} or line.startswith("Last Update:"):
            index += 1
            continue

        if section in {"Speeches", "FOMC Meetings"} and _looks_like_time(line):
            time_text = line
            title = _text(lines[index + 1]) if index + 1 < len(lines) else ""
            details: list[str] = []
            cursor = index + 2
            release_days: list[int] = []

            while cursor < len(lines):
                probe = lines[cursor]
                if probe in _FED_SECTION_NAMES or probe.startswith("Last Update:"):
                    break
                if _looks_like_time(probe):
                    break
                if _looks_like_day_list(probe):
                    release_days = _parse_day_values(probe)
                    cursor += 1
                    break
                if probe not in {"Watch Live", "PDF", "HTML", "Video", "Audio"}:
                    details.append(probe)
                cursor += 1

            for day in release_days:
                try:
                    local_date = date(year_number, month_number, day)
                except ValueError:
                    continue
                dt_utc = _combine_local_datetime(local_date, time_text, tz=ET)

                if section == "Speeches":
                    event_meta = _normalize_fed_speech_event(title)
                    if event_meta is None:
                        continue
                    event_name, impact = event_meta
                    events.append(
                        _build_event(
                            event=event_name,
                            category="fed",
                            impact=impact,
                            note=_fed_speech_note(details),
                            dt_utc=dt_utc,
                            date_local=local_date if dt_utc is None else None,
                            identity=f"{event_name.lower()}:{local_date.isoformat()}",
                            priority=70,
                        )
                    )
                    continue

                normalized_title = _text(title)
                if normalized_title == "FOMC Minutes":
                    detail = next((item for item in details if item.startswith("Meeting of ")), "")
                    note = detail or "Scheduled FOMC minutes release."
                    events.append(
                        _build_event(
                            event="FOMC Minutes",
                            category="fed",
                            impact="medium",
                            note=note,
                            dt_utc=dt_utc,
                            date_local=local_date if dt_utc is None else None,
                            identity=f"fomc_minutes:{local_date.isoformat()}",
                            priority=70,
                        )
                    )
                elif normalized_title == "FOMC Press Conference":
                    events.append(
                        _build_event(
                            event="FOMC Press Conference",
                            category="fed",
                            impact="high",
                            note="Scheduled FOMC press conference.",
                            dt_utc=dt_utc,
                            date_local=local_date if dt_utc is None else None,
                            identity=f"fomc_press_conference:{local_date.isoformat()}",
                            priority=70,
                        )
                    )
                elif normalized_title == "FOMC Meeting":
                    meeting_detail = next((item for item in details if item.startswith("Two-day meeting")), "")
                    note = meeting_detail or "Scheduled FOMC meeting."
                    events.append(
                        _build_event(
                            event="FOMC Meeting",
                            category="fed",
                            impact="high",
                            note=note,
                            dt_utc=dt_utc,
                            date_local=local_date if dt_utc is None else None,
                            identity=f"fomc_meeting:{local_date.isoformat()}",
                            priority=70,
                        )
                    )

            index = cursor
            continue

        index += 1
    return events


def _month_labels_for_horizon(now_utc: datetime, horizon_days: int) -> list[str]:
    start_local = now_utc.astimezone(ET).date().replace(day=1)
    cutoff_local = (now_utc + timedelta(days=max(int(horizon_days), 0) + 31)).astimezone(ET).date()
    labels: list[str] = []
    year = start_local.year
    month = start_local.month
    while (year, month) <= (cutoff_local.year, cutoff_local.month):
        labels.append(f"{_MONTH_NAMES[month - 1]} {year}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return labels


def _read_bls_schedule_events(
    source: dict,
    *,
    fetch_text_url,
    fetch_json_url,
    timeout: int,
    now_utc: datetime,
    horizon_days: int,
) -> tuple[list[dict], list[str]]:
    _ = fetch_json_url, now_utc, horizon_days
    raw_html = fetch_text_url(_text(source.get("url")), timeout=timeout)
    events = _parse_bls_schedule_html(
        raw_html,
        event=_text(source.get("event")),
        category=_text(source.get("category")) or "macro",
        impact=_text(source.get("impact")) or "medium",
        note=_text(source.get("note")) or "Scheduled BLS release.",
        priority=int(source.get("priority") or 0),
    )
    return events, []


def _read_bea_release_dates_events(
    source: dict,
    *,
    fetch_text_url,
    fetch_json_url,
    timeout: int,
    now_utc: datetime,
    horizon_days: int,
) -> tuple[list[dict], list[str]]:
    _ = fetch_text_url, now_utc, horizon_days
    payload = fetch_json_url(_text(source.get("url")), timeout=timeout)
    return _parse_bea_release_dates_json(payload), []


def _read_fed_fomc_events(
    source: dict,
    *,
    fetch_text_url,
    fetch_json_url,
    timeout: int,
    now_utc: datetime,
    horizon_days: int,
) -> tuple[list[dict], list[str]]:
    _ = fetch_json_url, now_utc, horizon_days
    raw_html = fetch_text_url(_text(source.get("url")), timeout=timeout)
    return _parse_fed_fomc_calendar_html(raw_html), []


def _read_fed_calendar_events(
    source: dict,
    *,
    fetch_text_url,
    fetch_json_url,
    timeout: int,
    now_utc: datetime,
    horizon_days: int,
) -> tuple[list[dict], list[str]]:
    _ = fetch_json_url
    base_url = _text(source.get("url"))
    raw_index_html = fetch_text_url(base_url, timeout=timeout)
    links = _parse_fed_calendar_index_html(raw_index_html, base_url=base_url)
    target_labels = _month_labels_for_horizon(now_utc, horizon_days)
    events: list[dict] = []
    errors: list[str] = []
    success_count = 0

    for label in target_labels:
        month_url = links.get(label)
        if not month_url:
            errors.append(f"missing calendar link for {label}")
            continue
        try:
            raw_month_html = fetch_text_url(month_url, timeout=timeout)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue
        success_count += 1
        events.extend(_parse_fed_calendar_month_html(raw_month_html))

    if success_count == 0:
        raise RuntimeError("unable to fetch any Federal Reserve monthly calendar pages")
    return events, errors


def build_event_calendar_payload(
    *,
    now_utc: datetime | None = None,
    sources: list[dict] | None = None,
    fetch_text_url=None,
    fetch_json_url=None,
    horizon_days: int = 7,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[dict | None, list[str]]:
    now = now_utc or datetime.now(UTC)
    source_defs = sources if sources is not None else _default_sources(now)
    text_fetcher = fetch_text_url or _fetch_text_url
    json_fetcher = fetch_json_url or _fetch_json_url

    events: list[dict] = []
    errors: list[str] = []
    success_count = 0

    for source in source_defs:
        reader = source.get("reader")
        name = _text(source.get("name")) or "source"
        if not callable(reader):
            errors.append(f"{name}: missing reader")
            continue
        try:
            source_events, source_errors = reader(
                source,
                fetch_text_url=text_fetcher,
                fetch_json_url=json_fetcher,
                timeout=timeout,
                now_utc=now,
                horizon_days=horizon_days,
            )
            success_count += 1
            events.extend(source_events or [])
            errors.extend(f"{name}: {message}" for message in source_errors if _text(message))
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if success_count == 0:
        return None, errors

    payload = normalize_event_calendar(
        {
            "generated_at_utc": utc_now_iso(),
            "events": _dedupe_events(events),
        }
    )
    payload["events"] = select_calendar_events(
        payload,
        now_msk=now.astimezone(MSK),
        horizon_hours=max(int(horizon_days), 0) * 24,
    )
    return payload, errors


def refresh_event_calendar(
    output_path: Path | None = None,
    *,
    now_utc: datetime | None = None,
    sources: list[dict] | None = None,
    fetch_text_url=None,
    fetch_json_url=None,
    horizon_days: int = 7,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[dict, bool, list[str]]:
    path = output_path or DEFAULT_EVENT_CALENDAR_PATH
    try:
        payload, errors = build_event_calendar_payload(
            now_utc=now_utc,
            sources=sources,
            fetch_text_url=fetch_text_url,
            fetch_json_url=fetch_json_url,
            horizon_days=horizon_days,
            timeout=timeout,
        )
    except Exception as exc:
        payload = None
        errors = [f"updater: {exc}"]

    if payload is None:
        if path.exists():
            return read_event_calendar(path), False, errors
        payload = normalize_event_calendar({"generated_at_utc": utc_now_iso(), "events": []})

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload, True, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_EVENT_CALENDAR_PATH))
    parser.add_argument("--horizon-days", type=int, default=7)
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    args = parser.parse_args()

    output_path = Path(args.output)
    try:
        payload, wrote, errors = refresh_event_calendar(
            output_path,
            horizon_days=args.horizon_days,
            timeout=args.timeout,
        )
        action = "updated" if wrote else "preserved"
    except Exception as exc:
        payload = read_event_calendar(output_path) if output_path.exists() else normalize_event_calendar(
            {"generated_at_utc": utc_now_iso(), "events": []}
        )
        wrote = False
        action = "preserved"
        errors = [f"updater: {exc}"]

    summary = build_calendar_context(
        "mid",
        path=output_path,
        now_msk=datetime.now(MSK),
        max_events=3,
    )
    print(
        json.dumps(
            {
                "status": action,
                "path": str(output_path),
                "generated_at_utc": payload.get("generated_at_utc"),
                "events_count": len(payload.get("events") or []),
                "sample_events": summary.get("calendar_events") or [],
                "errors": errors,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
