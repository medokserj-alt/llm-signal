#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import subprocess
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

DEFAULT_TARGET_CHAT_IDS = [-1003492385200, -1003493070625, -1003530482991]
DEFAULT_START_DATE = "2026-06-05"
DEFAULT_SIGNAL_SLOTS = ["09:30", "12:30", "15:30", "18:30", "21:30", "00:30"]
SCHEDULER_UID = -9000605


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
        )


def publish_decision_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_publish_decisions_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


def signal_decision_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"scheduled_signal_decisions_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"


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


async def generate_and_publish_signal(selected_mode: str, cfg: SchedulerConfig, *, dry_run: bool = False) -> dict:
    import tg_bot

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
        }

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
            text=parts[0],
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

    published_at = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    signal_id = tg_bot._infer_signal_id(Path(sig_html), Path(run_log) if run_log else None, published_at)
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
        aia_forward = await forward_signal_to_aia_awaited(tg_bot, signal_json_v1)
    return {
        "published": bool(ok),
        "reason": "published" if ok else "system_routing_api_error",
        "signal_id": signal_id if ok else None,
        "artifact_path": relpath(Path(sig_html)),
        "run_log": relpath(Path(run_log)) if run_log else None,
        "last_payload": payload,
        **aia_forward,
        "aia_forward_warning": "aia_forward_failed" if ok and not aia_forward.get("aia_forward_ok") else None,
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
    if isinstance(current, dict) and current.get("status") in {"published", "macro_substitution"}:
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
        result = await generate_and_publish_signal(str(gate["selected_mode"]), cfg, dry_run=dry_run)
        if result.get("published"):
            slots[slot_id] = {
                "slot_id": slot_id,
                "slot_time_msk": slot_time.isoformat(),
                "attempt": attempt,
                "status": "published",
                "next_retry_at_msk": None,
                "reason": result.get("reason"),
                "selected_mode": gate["selected_mode"],
                "signal_id": result.get("signal_id"),
            }
            row.update(
                {
                    "decision": "publish",
                    "reason": result.get("reason"),
                    "signal_id": result.get("signal_id"),
                    "aia_forward_attempted": bool(result.get("aia_forward_attempted")),
                    "aia_forward_ok": bool(result.get("aia_forward_ok")),
                    "aia_forward_error": result.get("aia_forward_error"),
                    "aia_forward_mode": result.get("aia_forward_mode"),
                    "aia_forward_warning": result.get("aia_forward_warning"),
                }
            )
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
