#!/usr/bin/env python3
from zoneinfo import ZoneInfo
import asyncio
import sys
import os, json, time, subprocess, re, html as htmllib, tempfile, copy
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest, parse as urlparse

MSK = ZoneInfo("Europe/Moscow")

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
except ImportError:
    class _TelegramStub:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyFilter:
        def __and__(self, other):
            return self

        def __rand__(self, other):
            return self

    class _Filters:
        TEXT = _DummyFilter()

        @staticmethod
        def Regex(pattern):
            return _DummyFilter()

    class _ContextTypes:
        DEFAULT_TYPE = object

    class _ParseMode:
        HTML = "HTML"

    Update = _TelegramStub
    KeyboardButton = _TelegramStub
    ReplyKeyboardMarkup = _TelegramStub
    InlineKeyboardButton = _TelegramStub
    InlineKeyboardMarkup = _TelegramStub
    Application = _TelegramStub
    CommandHandler = _TelegramStub
    MessageHandler = _TelegramStub
    CallbackQueryHandler = _TelegramStub
    ContextTypes = _ContextTypes
    ParseMode = _ParseMode
    filters = _Filters()

from pinned_state import get_pinned_message_id, load_pinned_state, save_pinned_state, set_pinned_message_id
from rbac import analysis_menu_layout, is_admin
from paid_allowlist import add_paid_allowed_uid, get_paid_allowed_ids
from user_registry import (
    consume_trial,
    get_all_users,
    get_user,
    is_active,
    register_user,
    set_signal_bot_started,
    set_paid,
    set_status,
    start_trial,
    status_text,
)

# ============== BASE / ENV ==================

BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE
LOGS_DIR = PROJECT_ROOT / "logs"
AGENT_STATE_REPO_ROOT = Path(os.getenv("AGENT_STATE_REPO_ROOT", "/root/llm-signal-ai-agent"))
DEFAULT_MANUAL_STATE_GUARD_STATE_PATH = AGENT_STATE_REPO_ROOT / "logs/agent_trade_state.json"
FIXED_BOT_ASSETS = ["BTC", "ETH", "BNB", "SOL", "XRP"]

if load_dotenv is not None:
    load_dotenv(BASE / ".env.tg.clean")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FALLBACK_CHANNEL = os.getenv("TELEGRAM_TARGET_CHANNEL")
SIGNAL_BOT_TOKEN = os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN")
SIGNAL_BOT_USERNAME = os.getenv("TELEGRAM_SIGNAL_BOT_USERNAME", "LLM_signals_pa_dev_bot")
ENV_PATH = BASE / ".env.tg.clean"
PANEL_SYMBOLS_MAX = int(os.getenv("TELEGRAM_PANEL_SYMBOLS_MAX", "18") or "18")

MANUAL_STATE_GUARD_SHADOW_ENABLED = os.getenv("MANUAL_STATE_GUARD_SHADOW_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MANUAL_STATE_GUARD_ENFORCEMENT_ENABLED = os.getenv("MANUAL_STATE_GUARD_ENFORCEMENT_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MANUAL_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED = os.getenv(
    "MANUAL_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
MANUAL_STATE_GUARD_DUPLICATE_WAIT_CONFIRM_ENFORCEMENT = os.getenv(
    "MANUAL_STATE_GUARD_DUPLICATE_WAIT_CONFIRM_ENFORCEMENT",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
MANUAL_STATE_GUARD_MAX_AGE_MINUTES = max(
    1,
    int(os.getenv("MANUAL_STATE_GUARD_MAX_AGE_MINUTES", os.getenv("STATE_GUARD_MAX_AGE_MINUTES", "15")) or "15"),
)
MANUAL_STATE_GUARD_STATE_PATH = Path(
    os.getenv("MANUAL_STATE_GUARD_STATE_PATH", os.getenv("STATE_GUARD_STATE_PATH", str(DEFAULT_MANUAL_STATE_GUARD_STATE_PATH)))
)
MANUAL_STATE_GUARD_DUPLICATE_ENTRY_DISTANCE_PCT = float(
    os.getenv("MANUAL_STATE_GUARD_DUPLICATE_ENTRY_DISTANCE_PCT", "0.5") or "0.5"
)
MANUAL_STATE_GUARD_DUPLICATE_SL_DISTANCE_PCT = float(
    os.getenv("MANUAL_STATE_GUARD_DUPLICATE_SL_DISTANCE_PCT", "1.0") or "1.0"
)

# ============== AIA (AI Agent) ==============

AIA_BASE_URL = "http://127.0.0.1:8002"
AIA_TIMEOUT_SEC = 2.5
AIA_TEST_CHANNEL_ID = os.getenv("AIA_TEST_CHANNEL_ID")
AIA_FORWARD_ALLOWED_CHAT_IDS = os.getenv("AIA_FORWARD_ALLOWED_CHAT_IDS")

def _parse_env_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not s.lstrip("-").isdigit():
        return None
    try:
        return int(s)
    except Exception:
        return None


def _parse_env_int_set(raw: str | None) -> set[int]:
    out: set[int] = set()
    if raw is None:
        return out
    for chunk in str(raw).replace(";", ",").split(","):
        value = _parse_env_int(chunk)
        if value is not None:
            out.add(value)
    return out

_AIA_TEST_CHANNEL_ID_INT = _parse_env_int(AIA_TEST_CHANNEL_ID)
_AIA_FORWARD_ALLOWED_CHAT_ID_SET = _parse_env_int_set(AIA_FORWARD_ALLOWED_CHAT_IDS)
_SUBSCRIPTION_LIMIT = _parse_env_int(os.getenv("TELEGRAM_SUBSCRIPTION_LIMIT"))

def _should_send_to_aia_for_target(target) -> bool:
    allowed_targets = _AIA_FORWARD_ALLOWED_CHAT_ID_SET
    if allowed_targets:
        target_id = _parse_env_int(str(target)) if not isinstance(target, int) else int(target)
        return target_id in allowed_targets
    test_id = _AIA_TEST_CHANNEL_ID_INT
    if test_id is None:
        return True
    if isinstance(target, int):
        return target == test_id
    if target is None:
        return False
    s = str(target).strip()
    if s.lstrip("-").isdigit():
        try:
            return int(s) == test_id
        except Exception:
            return False
    return s == str(test_id)

# ============== ACCESS CONTROL ==============

def parse_allowed_ids():
    ids = []
    raw_multi  = os.getenv("TELEGRAM_ALLOWED_USER_IDS","")
    raw_single = os.getenv("TELEGRAM_ALLOWED_USER_ID","")
    for raw in (raw_multi, raw_single):
        for x in raw.replace(";",",").split(","):
            x=x.strip()
            if x and x.lstrip("-").isdigit():
                ids.append(int(x))
    return list(dict.fromkeys(ids))


ALLOWED_UIDS = parse_allowed_ids()
PAID_ALLOWED_UIDS = get_paid_allowed_ids()

USER_CHANNELS_PATH = PROJECT_ROOT / "user_channels.json"
SUBSCRIPTIONS_PATH = Path(
    os.getenv("TELEGRAM_SUBSCRIPTIONS_PATH", PROJECT_ROOT / "user_subscriptions.json")
)
PARAMS_PATH = PROJECT_ROOT / "params.json"
PINNED_STATE_PATH = PROJECT_ROOT / "pinned_state.json"
CORE_SIGNAL_STATE_PATH = Path(
    os.getenv("TELEGRAM_CORE_SIGNAL_STATE_PATH", PROJECT_ROOT / "core_signal_state.json")
)
CORE_SIGNAL_POLL_SEC = _parse_env_int(os.getenv("TELEGRAM_CORE_SIGNAL_POLL_SEC")) or 10
CORE_SIGNAL_WATCHER_MODE = str(os.getenv("TELEGRAM_CORE_SIGNAL_WATCHER_MODE", "") or "").strip().lower()
SHARED_MAIN_ROUTE_KEY = "shared_v3_mixed"
USER_CONFIG = {}
GLOBAL_SETTINGS = {"lock_timeout_sec": 120}
VALID_MODES = {"aggressive", "neutral", "conservative"}
_AIA_UID_CONTEXT = None
SUBSCRIBERS: set[int] = set()

def load_user_channels():
    global USER_CONFIG, GLOBAL_SETTINGS
    if USER_CHANNELS_PATH.exists():
        try:
            data = json.loads(USER_CHANNELS_PATH.read_text(encoding="utf-8"))
            USER_CONFIG = data.get("users",{})
            st = data.get("settings",{})
            if isinstance(st,dict):
                GLOBAL_SETTINGS.update(st)
        except Exception:
            USER_CONFIG = {}

load_user_channels()

def load_subscriptions() -> None:
    global SUBSCRIBERS
    SUBSCRIBERS = set()
    if not SUBSCRIPTIONS_PATH.exists():
        return
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    raw = data.get("subscribers")
    if not isinstance(raw, list):
        return
    out: set[int] = set()
    for val in raw:
        if isinstance(val, int) and not isinstance(val, bool):
            out.add(val)
        elif isinstance(val, str) and val.strip().lstrip("-").isdigit():
            try:
                out.add(int(val.strip()))
            except Exception:
                continue
    SUBSCRIBERS = out

def _save_subscriptions() -> None:
    data = {"subscribers": sorted(SUBSCRIBERS)}
    SUBSCRIPTIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def is_subscribed(uid: int) -> bool:
    return uid in SUBSCRIBERS

def subscribe_user(uid: int) -> bool:
    if uid in SUBSCRIBERS:
        return False
    SUBSCRIBERS.add(uid)
    _save_subscriptions()
    return True

def unsubscribe_user(uid: int) -> bool:
    if uid not in SUBSCRIBERS:
        return False
    SUBSCRIBERS.remove(uid)
    _save_subscriptions()
    return True

def get_signal_targets(uid: int):
    return [uid]

load_subscriptions()

def _load_core_signal_state() -> dict:
    if not CORE_SIGNAL_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(CORE_SIGNAL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _save_core_signal_state(state: dict) -> None:
    CORE_SIGNAL_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _get_core_user_targets() -> list[int]:
    out: set[int] = set()
    users = get_all_users()
    for uid_str, record in users.items():
        if not isinstance(uid_str, str) or not uid_str.lstrip("-").isdigit():
            continue
        if not is_active(record):
            continue
        uid = int(uid_str)
        if ALLOWED_UIDS and uid not in ALLOWED_UIDS and uid not in PAID_ALLOWED_UIDS:
            continue
        out.add(uid)
    return sorted(out)

def _normalize_chat_id(raw) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s and s.lstrip("-").isdigit():
            try:
                return int(s)
            except Exception:
                return None
    return None

def _normalize_chat_id_list(raw) -> list[int]:
    if isinstance(raw, str):
        items = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    out: list[int] = []
    seen: set[int] = set()
    for item in items:
        chat_id = _normalize_chat_id(item)
        if chat_id is None or chat_id in seen:
            continue
        seen.add(chat_id)
        out.append(chat_id)
    return out

def _global_setting_chat_ids(setting_key: str, *, env_key: str | None = None) -> list[int]:
    values = _normalize_chat_id_list(GLOBAL_SETTINGS.get(setting_key))
    if values:
        return values
    if env_key:
        return _normalize_chat_id_list(os.getenv(env_key))
    return []

def _get_main_fanout_targets(primary_chat_id: int | None) -> list[int]:
    if primary_chat_id is None:
        return []
    target_ids = _global_setting_chat_ids(
        "main_fanout_chat_ids",
        env_key="TELEGRAM_MAIN_FANOUT_CHAT_IDS",
    )
    if not target_ids:
        return []
    source_ids = _global_setting_chat_ids(
        "main_fanout_source_chat_ids",
        env_key="TELEGRAM_MAIN_FANOUT_SOURCE_CHAT_IDS",
    )
    if source_ids and primary_chat_id not in source_ids:
        return []
    return [chat_id for chat_id in target_ids if chat_id != primary_chat_id]

def _get_user_channels(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    channels = cfg.get("channels")
    return channels if isinstance(channels, dict) else {}

def get_shared_main_chat_id() -> int | None:
    return _normalize_chat_id(_get_user_channels(USER_CONFIG.get(SHARED_MAIN_ROUTE_KEY)).get("main_chat_id"))

def _get_user_route_key(cfg: dict | None) -> str | None:
    if not isinstance(cfg, dict):
        return None
    for key in ("route", "shared_route", "routing_key", "main_route"):
        raw = cfg.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    routing = cfg.get("routing")
    if isinstance(routing, dict):
        for key in ("main", "route", "key"):
            raw = routing.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None

def is_shared_main_route_uid(uid: int) -> bool:
    cfg = USER_CONFIG.get(str(uid))
    if not isinstance(cfg, dict):
        return get_shared_main_chat_id() is not None
    route_key = _get_user_route_key(cfg)
    if route_key == SHARED_MAIN_ROUTE_KEY:
        return True
    shared_chat_id = get_shared_main_chat_id()
    direct_chat_id = _normalize_chat_id(_get_user_channels(cfg).get("main_chat_id"))
    return shared_chat_id is not None and direct_chat_id == shared_chat_id

def get_main_publication_chat_id(uid: int) -> int | None:
    cfg = USER_CONFIG.get(str(uid))
    if not isinstance(cfg, dict):
        shared_chat_id = get_shared_main_chat_id()
        if shared_chat_id is not None:
            return shared_chat_id
        return _normalize_chat_id(FALLBACK_CHANNEL)
    if is_shared_main_route_uid(uid):
        shared_chat_id = get_shared_main_chat_id()
        if shared_chat_id is not None:
            return shared_chat_id
    direct_chat_id = _normalize_chat_id(_get_user_channels(cfg).get("main_chat_id"))
    if direct_chat_id is not None:
        return direct_chat_id
    shared_chat_id = get_shared_main_chat_id()
    if shared_chat_id is not None:
        return shared_chat_id
    return _normalize_chat_id(FALLBACK_CHANNEL)

def get_main_publication_targets(uid: int) -> list[int]:
    primary = get_main_publication_chat_id(uid)
    if primary is None:
        return []
    return [primary]

def get_core_broadcast_targets() -> list[int]:
    targets: set[int] = set()
    for uid in _get_core_user_targets():
        targets.update(get_main_publication_targets(uid))
    return sorted(targets)

def _is_core_signal_watcher_enabled() -> bool:
    return CORE_SIGNAL_WATCHER_MODE in {"1", "true", "yes", "on", "watch", "watcher", "broadcast"}

async def _broadcast_core_signal(app: Application, sig_html: Path) -> None:
    parts = html_file_to_tg_text(sig_html)
    if not parts:
        return
    targets = get_core_broadcast_targets()
    if not targets:
        return
    published_at = _utc_now_z()
    signal_id = _infer_signal_id(sig_html, None, published_at)
    symbol = _resolve_signal_symbol(None)
    for chat_id in targets:
        for idx, part in enumerate(parts):
            try:
                await app.bot.send_message(chat_id=chat_id, text=part)
                if idx == 0:
                    _log_signal_publication(
                        event="telegram_publish",
                        uid=None,
                        symbol=symbol,
                        mode="watcher",
                        target_chat_id=chat_id,
                        source="tg_bot.py:_broadcast_core_signal",
                        signal_id=signal_id,
                        delivery_kind="watcher",
                    )
            except Exception:
                continue

async def _watch_core_signals(app: Application) -> None:
    state = _load_core_signal_state()
    last_path = state.get("path")
    last_mtime = state.get("mtime")
    initialized = bool(last_path)

    while True:
        try:
            sig_html = latest("signal_*.html")
            if not initialized:
                if sig_html:
                    last_path = sig_html.as_posix()
                    last_mtime = sig_html.stat().st_mtime
                    _save_core_signal_state({"path": last_path, "mtime": last_mtime})
                    initialized = True
                await asyncio.sleep(CORE_SIGNAL_POLL_SEC)
                continue

            if sig_html:
                current_path = sig_html.as_posix()
                current_mtime = sig_html.stat().st_mtime
                if current_path != last_path or (
                    last_mtime is not None and current_mtime > last_mtime
                ):
                    await _broadcast_core_signal(app, sig_html)
                    last_path = current_path
                    last_mtime = current_mtime
                    _save_core_signal_state({"path": last_path, "mtime": last_mtime})
        except Exception:
            pass

        await asyncio.sleep(CORE_SIGNAL_POLL_SEC)

async def _post_init(app: Application) -> None:
    if _is_core_signal_watcher_enabled():
        asyncio.create_task(_watch_core_signals(app))

def get_user_cfg(uid:int):
    return USER_CONFIG.get(str(uid))

def get_main_chat_id(uid:int):
    routed_chat_id = get_main_publication_chat_id(uid)
    if routed_chat_id is not None:
        return routed_chat_id
    return FALLBACK_CHANNEL

async def _send_main_publication(
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    text: str,
    *,
    parse_mode: str | None = None,
    protect_content: bool = False,
) -> list[int]:
    targets = get_main_publication_targets(uid)
    if not targets:
        return []
    delivered: list[int] = []
    for chat_id in targets:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                protect_content=protect_content,
            )
            delivered.append(chat_id)
        except Exception:
            continue
    return delivered

def _get_publication_target(uid: int, delivery_kind: str) -> int | None:
    if delivery_kind == "personal":
        return uid
    return get_main_publication_chat_id(uid)

async def _deliver_publication_targets(
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    text: str,
    *,
    delivery_kind: str,
    parse_mode: str | None = None,
    protect_content: bool = False,
) -> list[int]:
    if delivery_kind == "personal":
        return [uid] if _send_personal(uid, text, parse_mode=parse_mode, protect_content=protect_content) else []
    return await _send_main_publication(
        context,
        uid,
        text,
        parse_mode=parse_mode,
        protect_content=protect_content,
    )

async def _deliver_publication(
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    text: str,
    *,
    delivery_kind: str,
    parse_mode: str | None = None,
    protect_content: bool = False,
) -> bool:
    sent = await _deliver_publication_targets(
        context,
        uid,
        text,
        delivery_kind=delivery_kind,
        parse_mode=parse_mode,
        protect_content=protect_content,
    )
    return bool(sent)

def get_all_main_channels() -> list[int]:
    out: set[int] = set()
    for cfg in USER_CONFIG.values():
        if not isinstance(cfg, dict):
            continue
        channels = cfg.get("channels")
        if not isinstance(channels, dict):
            continue
        ch = channels.get("main_chat_id")
        if isinstance(ch, int):
            out.add(ch)
        elif isinstance(ch, str) and ch.strip().lstrip("-").isdigit():
            out.add(int(ch.strip()))
    if FALLBACK_CHANNEL and str(FALLBACK_CHANNEL).strip().lstrip("-").isdigit():
        out.add(int(str(FALLBACK_CHANNEL).strip()))
    return sorted(out)

def get_min_interval(uid:int) -> int:
    cfg = get_user_cfg(uid)
    if cfg:
        lim = cfg.get("limits",{})
        v = lim.get("min_interval_sec")
        if isinstance(v,int):
            return v
    return 0

def get_user_mode(uid:int) -> str:
    cfg = get_user_cfg(uid)
    mode = None
    if cfg:
        m = cfg.get("mode")
        if m in VALID_MODES:
            mode = m
    return mode or "neutral"

def set_user_mode(uid:int, mode:str):
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode: {mode}")
    uid_str = str(uid)
    data = {}
    if USER_CHANNELS_PATH.exists():
        try:
            data = json.loads(USER_CHANNELS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    users = data.get("users")
    if not isinstance(users, dict):
        users = {}
    user_cfg = users.get(uid_str)
    if not isinstance(user_cfg, dict):
        user_cfg = {}

    user_cfg["mode"] = mode
    users[uid_str] = user_cfg
    data["users"] = users

    USER_CONFIG[uid_str] = user_cfg

    USER_CHANNELS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def is_allowed(uid:int) -> bool:
    if ALLOWED_UIDS or PAID_ALLOWED_UIDS:
        return uid in ALLOWED_UIDS or uid in PAID_ALLOWED_UIDS
    return True

# ============== HELPERS =====================

def make_header(title:str)->str:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    return f"{title} • {now.strftime('%d.%m.%Y %H:%M')}"

def latest(pattern:str):
    files = list(PROJECT_ROOT.glob(pattern))
    return max(files, key=lambda p:p.stat().st_mtime) if files else None

def _project_path_from_output(raw_path: str | None) -> Path | None:
    if not isinstance(raw_path, str):
        return None
    value = raw_path.strip()
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()

def _extract_signal_artifact_path(output: str | None, label: str) -> Path | None:
    if not isinstance(output, str) or not output.strip():
        return None
    prefix = f"{label}:"
    for line in reversed(output.splitlines()):
        if not line.startswith(prefix):
            continue
        return _project_path_from_output(line.split(":", 1)[1])
    return None

def _resolve_signal_run_artifacts(proc) -> tuple[Path | None, Path | None]:
    stdout = getattr(proc, "stdout", None)
    stderr = getattr(proc, "stderr", None)
    combined = "\n".join(part for part in (stdout, stderr) if isinstance(part, str) and part)
    run_log = _extract_signal_artifact_path(combined, "✅ Saved logs")
    sig_html = _extract_signal_artifact_path(combined, "✅ Signal HTML")
    return sig_html, run_log

def latest_report_dir(root_dir: str) -> Path | None:
    root = PROJECT_ROOT / "reports" / root_dir
    roots = sorted(root.glob("*"))
    return roots[-1] if roots else None

def strip_snapshot(text:str)->str:
    return re.sub(
        r"(?s)^=== \[SNAPSHOT ДЛЯ LLM\] ===.*?==========================\n?",
        "",
        text
    ).strip()

def html_file_to_tg_text(p:Path,max_len:int=4000):
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"<[^>]+>","",s)
    s = htmllib.unescape(s).strip()
    chunks=[]
    while s:
        chunks.append(s[:max_len])
        s=s[max_len:]
    return chunks

REPORT_TELEGRAM_MAX_LEN = 3900


def _split_text_for_telegram_sections(text: str, max_len: int = REPORT_TELEGRAM_MAX_LEN) -> list[str]:
    clean = str(text or "").strip()
    if not clean:
        return []
    if len(clean) <= max_len:
        return [clean]

    def _split_long_block(block: str) -> list[str]:
        lines = [ln.rstrip() for ln in block.splitlines()]
        if len(lines) > 1:
            out: list[str] = []
            current = ""
            for line in lines:
                candidate = line if not current else current + "\n" + line
                if len(candidate) <= max_len:
                    current = candidate
                    continue
                if current:
                    out.append(current)
                current = line
            if current:
                out.append(current)
            if all(len(item) <= max_len for item in out):
                return out

        words = block.split()
        if not words:
            return []
        out = []
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if len(candidate) <= max_len:
                current = candidate
                continue
            if current:
                out.append(current)
            current = word
        if current:
            out.append(current)
        return out

    sections = [section.strip() for section in re.split(r"\n\s*\n", clean) if section.strip()]
    blocks: list[str] = []
    for section in sections:
        if len(section) <= max_len:
            blocks.append(section)
        else:
            blocks.extend(_split_long_block(section))

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks


def _build_report_telegram_messages(root_dir: str, emoji: str, body_text: str) -> list[str]:
    hdr = make_header(f"{emoji} {root_dir.upper()}")
    clean = str(body_text or "").strip()
    if not clean:
        return []

    title_prefix = f"{emoji} {root_dir.upper()}"
    chunks = _split_text_for_telegram_sections(clean, max_len=REPORT_TELEGRAM_MAX_LEN)
    if len(chunks) <= 1:
        return [hdr + "\n\n" + clean]

    total = len(chunks)
    messages: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        if idx == 1:
            title = f"{hdr} (part {idx}/{total})"
        elif idx == total:
            title = f"{title_prefix} • appendix (part {idx}/{total})"
        else:
            title = f"{title_prefix} • part {idx}/{total}"
        messages.append(title + "\n\n" + chunk)
    return messages

def _relpath(p: Path) -> str:
    try:
        return p.relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return p.as_posix()

def _resolve_signal_symbol(symbol_hint: str | None) -> str | None:
    if isinstance(symbol_hint, str) and symbol_hint.strip():
        return symbol_hint.strip()
    last = _read_last_signal_json()
    if not isinstance(last, dict):
        return None
    for key in ("symbol", "asset"):
        raw = last.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None

def _log_signal_publication(
    *,
    event: str,
    uid: int | None,
    symbol: str | None,
    mode: str | None,
    target_chat_id: int | None,
    source: str,
    signal_id: str | None,
    delivery_kind: str,
    manual_state_guard: dict | None = None,
) -> None:
    record = {
        "event": event,
        "uid": uid,
        "symbol": symbol,
        "mode": mode,
        "target_chat_id": target_chat_id,
        "source": source,
        "signal_id": signal_id,
        "delivery_kind": delivery_kind,
        "ts": _utc_now_z(),
    }
    if isinstance(manual_state_guard, dict):
        record.update(manual_state_guard)
    print("[signal_publish] " + json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)

def format_done(done_line: str, logs_path: str | None = None) -> str:
    msg = "Готово.\n<pre>" + htmllib.escape(done_line) + "</pre>"
    if logs_path:
        msg += "\nЛоги сохранены: <code>" + htmllib.escape(logs_path) + "</code>"
    return msg

def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

def _signal_id_from_published_at_msk(published_at: str) -> str:
    try:
        dt_utc = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(MSK).strftime("%Y%m%d_%H%M%S")
    except Exception:
        return datetime.now(MSK).strftime("%Y%m%d_%H%M%S")

def _infer_signal_id(sig_html: Path | None, run_log: Path | None, published_at: str) -> str:
    for p in (sig_html, run_log):
        if not p:
            continue
        m = re.search(r"_(\d{8}_\d{6})\.", p.name)
        if m:
            return m.group(1)
    return _signal_id_from_published_at_msk(published_at)

def _aia_request_json(method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> tuple[int, dict | None]:
    url = AIA_BASE_URL.rstrip("/") + path
    if query:
        url += "?" + urlparse.urlencode(query, doseq=True)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urlrequest.Request(url, data=data, method=method.upper(), headers=headers)
    with urlrequest.urlopen(req, timeout=AIA_TIMEOUT_SEC) as resp:
        status = int(getattr(resp, "status", 200))
        raw = resp.read() or b""
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return status, None

def _signal_bot_request_json(method: str, path: str, *, body: dict | None = None) -> bool:
    if not SIGNAL_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{SIGNAL_BOT_TOKEN}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        req = urlrequest.Request(url, data=data, method=method.upper(), headers=headers)
        with urlrequest.urlopen(req, timeout=10) as resp:
            status = int(getattr(resp, "status", 200))
            return 200 <= status < 300
    except Exception:
        return False

def _signal_bot_hint_text() -> str:
    name = (SIGNAL_BOT_USERNAME or "LLM_signals_pa_dev_bot").lstrip("@")
    return f"Чтобы получать результаты, открой @{name} и нажми /start."

def _signal_bot_link() -> str:
    name = (SIGNAL_BOT_USERNAME or "LLM_signals_pa_dev_bot").lstrip("@")
    return f"https://t.me/{name}?start=1"

def _signal_bot_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Открыть Signal bot", url=_signal_bot_link())]])

async def _send_signal_bot_hint(update: Update) -> None:
    await update.message.reply_text(_signal_bot_hint_text(), reply_markup=_signal_bot_kb())

def _send_via_signal_bot(chat_id: int, text: str, *, parse_mode: str | None = None, protect_content: bool = False) -> bool:
    payload = {"chat_id": chat_id, "text": text, "protect_content": bool(protect_content)}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _signal_bot_request_json("POST", "/sendMessage", body=payload)

def _send_personal(uid: int, text: str, *, parse_mode: str | None = None, protect_content: bool = False) -> bool:
    ok = _send_via_signal_bot(uid, text, parse_mode=parse_mode, protect_content=protect_content)
    if ok:
        try:
            set_signal_bot_started(uid, True)
        except Exception:
            pass
    return ok

def _read_last_signal_json(path: Path | None = None) -> dict | None:
    p = path if path is not None else (PROJECT_ROOT / "logs" / "last.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _jsonl_append(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    except Exception:
        pass

def _manual_state_guard_shadow_log_path(now_utc: datetime) -> Path:
    return LOGS_DIR / f"manual_state_guard_shadow_{now_utc.astimezone(MSK).strftime('%Y%m%d')}.jsonl"

def _manual_state_guard_base(enabled: bool, status: str) -> dict:
    return {
        "manual_state_guard_shadow_enabled": enabled,
        "manual_state_guard_status": status,
        "manual_state_guard_decision": None,
        "manual_state_guard_can_publish_full_signal": None,
        "manual_state_guard_recommended_publication_type": None,
        "manual_state_guard_reason": None,
        "manual_state_guard_primary_signal_id": None,
        "manual_state_guard_primary_lifecycle_state": None,
        "manual_state_guard_primary_position_status": None,
        "manual_state_guard_duplicate_detected": None,
        "manual_state_guard_conflict_detected": None,
        "manual_state_guard_replacement_candidate": None,
        "manual_state_guard_entry_distance_pct": None,
        "manual_state_guard_sl_distance_pct": None,
        "manual_state_guard_secondary_signal_ids": [],
        "manual_state_guard_explanation": None,
        "manual_state_guard_previous_signal_id": None,
        "manual_state_guard_previous_outcome": None,
        "manual_state_guard_previous_tp_reached": None,
        "manual_state_guard_previous_runner_status": None,
        "manual_state_guard_reentry_signal": None,
        "manual_state_guard_reentry_allowed": None,
        "manual_state_guard_reentry_reason": None,
        "manual_state_guard_duplicate_enforcement_enabled": MANUAL_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED,
        "manual_state_guard_duplicate_runtime_eligible": False,
        "manual_state_guard_enforcement_action": "none",
        "manual_state_guard_suppression_reason": None,
    }

def _manual_state_guard_try_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None

def _manual_state_guard_signal_rr(payload: dict, mode: str | None) -> float | None:
    rr_by_mode = payload.get("rr_by_mode")
    if isinstance(rr_by_mode, dict) and mode:
        value = _manual_state_guard_try_float(rr_by_mode.get(mode))
        if value is not None:
            return value
    return _manual_state_guard_try_float(payload.get("rr"))

def _manual_state_guard_candidate_from_payload(
    payload: dict | None,
    *,
    signal_id: str,
    symbol_hint: str | None,
    mode: str | None,
    created_at: str,
) -> dict | None:
    if not isinstance(payload, dict):
        return None
    symbol = payload.get("symbol") or payload.get("asset") or symbol_hint
    direction = payload.get("direction") or payload.get("side")
    entry = _manual_state_guard_try_float(payload.get(f"entry_price_{mode}")) if mode else None
    if entry is None:
        entry = _manual_state_guard_try_float(payload.get("entry_price"))
    if entry is None:
        entry_range = payload.get("entry_range")
        if isinstance(entry_range, dict):
            low = _manual_state_guard_try_float(entry_range.get("min"))
            high = _manual_state_guard_try_float(entry_range.get("max"))
        elif isinstance(entry_range, (list, tuple)) and len(entry_range) == 2:
            low = _manual_state_guard_try_float(entry_range[0])
            high = _manual_state_guard_try_float(entry_range[1])
        else:
            low = high = None
        if low is not None and high is not None:
            entry = (low + high) / 2.0
    sl = None
    sl_by_mode = payload.get("sl_by_mode")
    if isinstance(sl_by_mode, dict) and mode:
        sl = _manual_state_guard_try_float(sl_by_mode.get(mode))
    if sl is None:
        sl = _manual_state_guard_try_float(payload.get("sl"))

    tp1 = _manual_state_guard_try_float(payload.get("tp1"))
    tp2 = _manual_state_guard_try_float(payload.get("tp2"))
    tp3 = _manual_state_guard_try_float(payload.get("tp3"))
    tp_by_mode = payload.get("tp_by_mode")
    if isinstance(tp_by_mode, dict) and mode and isinstance(tp_by_mode.get(mode), dict):
        mode_tp = tp_by_mode[mode]
        tp1 = _manual_state_guard_try_float(mode_tp.get("tvh1") or mode_tp.get("tp1")) or tp1
        tp2 = _manual_state_guard_try_float(mode_tp.get("tvh2") or mode_tp.get("tp2")) or tp2
        tp3 = _manual_state_guard_try_float(mode_tp.get("tvh3") or mode_tp.get("tp3")) or tp3

    if not symbol or not direction:
        return None
    return {
        "signal_id": signal_id,
        "symbol": str(symbol),
        "direction": str(direction).strip().lower(),
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "mode": mode,
        "source": "manual",
        "created_at": created_at,
        "rr": _manual_state_guard_signal_rr(payload, mode),
        "strategy_type": payload.get("strategy_type") or payload.get("strategy"),
    }

def _load_manual_state_guard_api():
    repo_root = str(AGENT_STATE_REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from app.state.state_query import evaluate_candidate_signal

    return evaluate_candidate_signal

def _evaluate_manual_state_guard(candidate: dict | None, *, now_utc: datetime) -> dict:
    if not MANUAL_STATE_GUARD_SHADOW_ENABLED:
        return _manual_state_guard_base(False, "disabled")
    out = _manual_state_guard_base(True, "unavailable")
    if not isinstance(candidate, dict):
        out["manual_state_guard_status"] = "error"
        out["manual_state_guard_explanation"] = "manual candidate unavailable"
        return out
    try:
        stat = MANUAL_STATE_GUARD_STATE_PATH.stat()
    except FileNotFoundError:
        return out
    except Exception as exc:
        out["manual_state_guard_explanation"] = f"state file unavailable: {exc}"
        return out
    age_seconds = max(0.0, now_utc.timestamp() - stat.st_mtime)
    if age_seconds > MANUAL_STATE_GUARD_MAX_AGE_MINUTES * 60:
        out["manual_state_guard_status"] = "stale"
        out["manual_state_guard_explanation"] = (
            f"state file older than {MANUAL_STATE_GUARD_MAX_AGE_MINUTES} minutes "
            f"({round(age_seconds / 60.0, 2)} minutes)"
        )
        return out
    try:
        state = json.loads(MANUAL_STATE_GUARD_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        out["manual_state_guard_explanation"] = f"state file unavailable: {exc}"
        return out
    if not isinstance(state, dict):
        out["manual_state_guard_explanation"] = "state file did not contain a JSON object"
        return out
    try:
        evaluation = _load_manual_state_guard_api()(candidate, state)
    except Exception as exc:
        out["manual_state_guard_status"] = "error"
        out["manual_state_guard_explanation"] = str(exc)
        return out
    out.update(
        {
            "manual_state_guard_status": "ok",
            "manual_state_guard_decision": evaluation.get("decision"),
            "manual_state_guard_can_publish_full_signal": evaluation.get("can_publish_full_signal"),
            "manual_state_guard_recommended_publication_type": evaluation.get("recommended_publication_type"),
            "manual_state_guard_reason": evaluation.get("reason"),
            "manual_state_guard_primary_signal_id": evaluation.get("primary_signal_id"),
            "manual_state_guard_primary_lifecycle_state": evaluation.get("primary_lifecycle_state"),
            "manual_state_guard_primary_position_status": evaluation.get("primary_position_status"),
            "manual_state_guard_duplicate_detected": evaluation.get("duplicate_detected"),
            "manual_state_guard_conflict_detected": evaluation.get("conflict_detected"),
            "manual_state_guard_replacement_candidate": evaluation.get("replacement_candidate"),
            "manual_state_guard_entry_distance_pct": evaluation.get("entry_distance_pct"),
            "manual_state_guard_sl_distance_pct": evaluation.get("sl_distance_pct"),
            "manual_state_guard_secondary_signal_ids": evaluation.get("secondary_signal_ids") or [],
            "manual_state_guard_explanation": evaluation.get("explanation"),
            "manual_state_guard_previous_signal_id": evaluation.get("previous_signal_id"),
            "manual_state_guard_previous_outcome": evaluation.get("previous_outcome"),
            "manual_state_guard_previous_tp_reached": evaluation.get("previous_tp_reached"),
            "manual_state_guard_previous_runner_status": evaluation.get("previous_runner_status"),
            "manual_state_guard_reentry_signal": evaluation.get("reentry_signal"),
            "manual_state_guard_reentry_allowed": evaluation.get("reentry_allowed"),
            "manual_state_guard_reentry_reason": evaluation.get("reentry_reason"),
        }
    )
    return out

def _manual_state_guard_duplicate_runtime(result: dict) -> dict:
    decision = str(result.get("manual_state_guard_decision") or "").strip().upper()
    lifecycle = str(result.get("manual_state_guard_primary_lifecycle_state") or "").strip().upper()
    position = str(result.get("manual_state_guard_primary_position_status") or "").strip().upper()
    entry_distance = _manual_state_guard_try_float(result.get("manual_state_guard_entry_distance_pct"))
    sl_distance = _manual_state_guard_try_float(result.get("manual_state_guard_sl_distance_pct"))
    terminal = lifecycle in {"TERMINAL", "CLOSED", "SL_HIT_LIVE", "FINALIZE"} or position == "CLOSED"
    eligible = (
        result.get("manual_state_guard_status") == "ok"
        and decision in {"MANAGEMENT_UPDATE", "ACTIVE_SIGNAL_UPDATE", "SUPPRESS_DUPLICATE"}
        and result.get("manual_state_guard_can_publish_full_signal") is False
        and result.get("manual_state_guard_duplicate_detected") is True
        and bool(result.get("manual_state_guard_primary_signal_id"))
        and not terminal
        and entry_distance is not None
        and entry_distance <= MANUAL_STATE_GUARD_DUPLICATE_ENTRY_DISTANCE_PCT
        and sl_distance is not None
        and sl_distance <= MANUAL_STATE_GUARD_DUPLICATE_SL_DISTANCE_PCT
    )
    action = "none"
    reason = None
    if eligible and MANUAL_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED:
        action = "convert_to_management_update" if decision == "MANAGEMENT_UPDATE" else "suppress_duplicate"
        reason = str(result.get("manual_state_guard_reason") or "same_direction_duplicate_active_scenario")
    return {
        "manual_state_guard_duplicate_enforcement_enabled": MANUAL_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED,
        "manual_state_guard_duplicate_runtime_eligible": eligible,
        "manual_state_guard_enforcement_action": action,
        "manual_state_guard_suppression_reason": reason,
    }

def _manual_state_guard_legacy_wait_confirm_action(result: dict) -> str:
    if not MANUAL_STATE_GUARD_ENFORCEMENT_ENABLED or not MANUAL_STATE_GUARD_DUPLICATE_WAIT_CONFIRM_ENFORCEMENT:
        return "none"
    if result.get("manual_state_guard_status") != "ok":
        return "none"
    if result.get("manual_state_guard_can_publish_full_signal") is not False:
        return "none"
    if result.get("manual_state_guard_duplicate_detected") is not True:
        return "none"
    primary_lifecycle = str(result.get("manual_state_guard_primary_lifecycle_state") or "").upper()
    primary_position = str(result.get("manual_state_guard_primary_position_status") or "").upper()
    if primary_lifecycle not in {"WAIT_CONFIRM", "WAIT_POST_EVENT_REPRICE"}:
        return "none"
    if primary_position in {"OPEN", "PARTIALLY_REDUCED", "RUNNER_ACTIVE"}:
        return "none"
    if result.get("manual_state_guard_replacement_candidate") is True:
        return "replace_wait_confirm"
    return "duplicate_wait_confirm_suppressed"

def render_state_guard_classification_message(result: dict, *, symbol: str | None = None) -> str:
    decision = str(
        result.get("manual_state_guard_decision")
        or result.get("state_guard_decision")
        or result.get("decision")
        or ""
    ).strip().upper()
    display_symbol = (
        symbol
        or result.get("symbol")
        or result.get("display_symbol")
        or "этому инструменту"
    )
    direction = str(result.get("direction") or "").strip().upper()
    market = f"{display_symbol} {direction}".strip()
    if decision == "MANAGEMENT_UPDATE":
        return (
            f"🔄 MANAGEMENT_UPDATE\n\nЭто не новый вход. По {market} уже есть активный/managed сценарий. "
            "Новый сигнал классифицирован как management update: сопровождаем текущую позицию, "
            "не открываем независимый второй full signal."
        )
    if decision == "ACTIVE_SIGNAL_UPDATE":
        return (
            "🔄 ACTIVE_SIGNAL_UPDATE\n\nЭто обновление активного сценария, не новый full signal. "
            "Старый сигнал остаётся основным, новые уровни/контекст используются как update."
        )
    if decision == "RE_ENTRY_SIGNAL":
        return (
            "🔁 RE_ENTRY_SIGNAL\n\nЭто continuation re-entry после частичной фиксации предыдущего сценария. "
            "Предыдущий сигнал уже взял TP1/TP2 или был закрыт/сокращён. Новый вход допустим только как "
            "отдельная сделка после fresh pullback/reset, не как безусловный добор старой позиции."
        )
    if decision == "SUPPRESS_DUPLICATE":
        return (
            "⛔ SUPPRESS_DUPLICATE\n\nПохожий сценарий уже активен. Новый full signal подавлен, "
            "чтобы не дублировать позицию."
        )
    return "Это не новый full signal."

def _manual_state_guard_jsonl_row(
    *,
    now_utc: datetime,
    request_type: str,
    candidate: dict | None,
    result: dict,
    enforcement_action: str,
) -> dict:
    candidate = candidate if isinstance(candidate, dict) else {}
    return {
        "ts": now_utc.isoformat().replace("+00:00", "Z"),
        "request_type": request_type,
        "candidate_signal_id": candidate.get("signal_id"),
        "symbol": candidate.get("symbol"),
        "direction": candidate.get("direction"),
        "candidate_entry": candidate.get("entry"),
        "candidate_sl": candidate.get("sl"),
        "candidate_rr": candidate.get("rr"),
        "guard_status": result.get("manual_state_guard_status"),
        "guard_decision": result.get("manual_state_guard_decision"),
        "can_publish_full_signal": result.get("manual_state_guard_can_publish_full_signal"),
        "recommended_publication_type": result.get("manual_state_guard_recommended_publication_type"),
        "reason": result.get("manual_state_guard_reason"),
        "primary_signal_id": result.get("manual_state_guard_primary_signal_id"),
        "primary_lifecycle_state": result.get("manual_state_guard_primary_lifecycle_state"),
        "primary_position_status": result.get("manual_state_guard_primary_position_status"),
        "duplicate_detected": result.get("manual_state_guard_duplicate_detected"),
        "conflict_detected": result.get("manual_state_guard_conflict_detected"),
        "replacement_candidate": result.get("manual_state_guard_replacement_candidate"),
        "previous_signal_id": result.get("manual_state_guard_previous_signal_id"),
        "previous_outcome": result.get("manual_state_guard_previous_outcome"),
        "previous_tp_reached": result.get("manual_state_guard_previous_tp_reached"),
        "previous_runner_status": result.get("manual_state_guard_previous_runner_status"),
        "reentry_signal": result.get("manual_state_guard_reentry_signal"),
        "reentry_allowed": result.get("manual_state_guard_reentry_allowed"),
        "reentry_reason": result.get("manual_state_guard_reentry_reason"),
        "explanation": result.get("manual_state_guard_explanation"),
        "entry_distance_pct": result.get("manual_state_guard_entry_distance_pct"),
        "sl_distance_pct": result.get("manual_state_guard_sl_distance_pct"),
        "enforcement_enabled": bool(
            MANUAL_STATE_GUARD_DUPLICATE_ENFORCEMENT_ENABLED
        ),
        "duplicate_runtime_eligible": result.get("manual_state_guard_duplicate_runtime_eligible"),
        "suppression_reason": result.get("manual_state_guard_suppression_reason"),
        "enforcement_action": enforcement_action,
    }

def _should_evaluate_manual_state_guard(source: str, delivery_kind: str, text: str) -> bool:
    if source.startswith("scheduled_runner.py:"):
        return False
    if "📌 Сигнал не выдан" in text:
        return False
    return delivery_kind in {"main", "personal"}

def _try_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        if float(v).is_integer():
            return int(v)
        return None
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        try:
            return int(v.strip())
        except Exception:
            return None
    return None

VALID_EVENT_RISK_LEVELS = {"low", "medium", "high", "severe"}


def _normalize_event_risk_level(raw) -> str:
    text = str(raw or "").strip().lower()
    return text if text in VALID_EVENT_RISK_LEVELS else "unknown"


def _event_risk_level_rank(raw) -> int:
    return {"low": 1, "medium": 2, "high": 3, "severe": 4}.get(_normalize_event_risk_level(raw), 0)


def _max_event_risk_level(*values) -> str:
    best = "unknown"
    best_rank = 0
    for value in values:
        normalized = _normalize_event_risk_level(value)
        rank = _event_risk_level_rank(normalized)
        if rank > best_rank:
            best = normalized
            best_rank = rank
    return best


def _infer_event_risk_level(source: dict, d: dict, signal_summary: dict) -> str:
    regime = d.get("event_risk_regime") if isinstance(d.get("event_risk_regime"), dict) else {}
    source_regime = source.get("event_risk_regime") if isinstance(source.get("event_risk_regime"), dict) else {}
    return _max_event_risk_level(
        source.get("event_risk_level"),
        d.get("event_risk_level"),
        regime.get("severity"),
        source_regime.get("severity"),
        signal_summary.get("volatility_risk"),
        signal_summary.get("execution_caution"),
    )


def _infer_event_bias(source: dict, d: dict) -> str | None:
    regime = d.get("event_risk_regime") if isinstance(d.get("event_risk_regime"), dict) else {}
    source_regime = source.get("event_risk_regime") if isinstance(source.get("event_risk_regime"), dict) else {}
    neutral_seen = False
    for candidate in (
        source.get("event_bias"),
        d.get("event_bias"),
        regime.get("directional_risk"),
        regime.get("event_bias"),
        source_regime.get("directional_risk"),
        source_regime.get("event_bias"),
    ):
        text = str(candidate or "").strip().lower()
        if text in {"risk_on", "risk_off", "uncertain", "mixed"}:
            return text
        if text == "neutral":
            neutral_seen = True
    if str(regime.get("risk_asymmetry") or source_regime.get("risk_asymmetry") or "").strip().lower() == "asymmetric_downside":
        return "risk_off"
    return "neutral" if neutral_seen else None


def _event_direction_compatibility(direction, event_bias) -> str | None:
    side = str(direction or "").strip().lower()
    bias = str(event_bias or "").strip().lower()
    if side not in {"long", "short"}:
        return None
    if bias in {"", "neutral", "unknown"}:
        return "neutral"
    if bias in {"mixed", "uncertain"}:
        return "unclear"
    if (bias == "risk_off" and side == "short") or (bias == "risk_on" and side == "long"):
        return "aligned"
    if (bias == "risk_off" and side == "long") or (bias == "risk_on" and side == "short"):
        return "conflicting"
    return None


def _build_aia_event_risk_payload(d: dict) -> dict:
    if not isinstance(d, dict):
        return {}
    source = d.get("event_risk") if isinstance(d.get("event_risk"), dict) else {}
    if not source:
        return {}
    out = copy.deepcopy(source)

    signal_summary = source.get("signal_summary") if isinstance(source.get("signal_summary"), dict) else {}
    for src_key, out_key in (
        ("dominant_driver", "dominant_driver"),
        ("dominant_phase", "dominant_phase"),
        ("volatility_risk", "volatility_risk"),
        ("execution_caution", "execution_caution"),
    ):
        if out.get(out_key) is None and signal_summary.get(src_key) is not None:
            out[out_key] = signal_summary.get(src_key)

    for key in (
        "event_risk_level",
        "event_bias",
        "confirm_policy",
        "risk_compatibility",
        "direction_event_compatibility",
        "confirm_profile_used",
        "headline_risk_active",
        "dominant_critical_topic",
        "critical_topics",
        "soft_veto_reason",
        "soft_veto_origin",
        "macro_risk_summary",
        "display_lines",
        "urgent_flag",
        "urgent_message",
        "generated_at",
        "timestamp_utc",
        "event_risk_context_timestamp_utc",
        "upcoming_events",
        "scheduled_macro_events",
    ):
        if out.get(key) is None and key in d and d.get(key) is not None:
            out[key] = copy.deepcopy(d.get(key))

    inferred_level = _infer_event_risk_level(source, d, signal_summary)
    if inferred_level != "unknown" and _event_risk_level_rank(inferred_level) > _event_risk_level_rank(out.get("event_risk_level")):
        out["event_risk_level"] = inferred_level
    elif out.get("event_risk_level") is not None:
        out["event_risk_level"] = _normalize_event_risk_level(out.get("event_risk_level"))

    inferred_bias = _infer_event_bias(source, d)
    if inferred_bias is not None and (out.get("event_bias") is None or str(out.get("event_bias")).strip().lower() == "neutral"):
        out["event_bias"] = inferred_bias
    inferred_compatibility = _event_direction_compatibility(d.get("direction") or d.get("side"), out.get("event_bias"))
    if inferred_compatibility and (out.get("direction_event_compatibility") is None or str(out.get("direction_event_compatibility")).strip().lower() == "neutral"):
        out["direction_event_compatibility"] = inferred_compatibility
    generated_at = out.get("event_risk_generated_at") or out.get("generated_at") or out.get("timestamp_utc")
    if generated_at is not None:
        out["event_risk_generated_at"] = generated_at
    if out.get("event_risk_context_timestamp_utc") is None and d.get("event_risk_context_timestamp_utc") is not None:
        out["event_risk_context_timestamp_utc"] = d.get("event_risk_context_timestamp_utc")
    if out.get("source") is None:
        out["source"] = "signal_core"
    return out

def _shorten_text(s: str, max_len: int = 220) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    return t[: max_len - 3].rstrip() + "..."

def _extract_confirm_text(d: dict) -> str:
    raw = d.get("confirmation_rules")
    if isinstance(raw, str):
        return _shorten_text(raw)
    if isinstance(raw, list):
        parts: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                parts.append(x.strip())
            elif isinstance(x, dict):
                txt = x.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        return _shorten_text("; ".join(parts))
    return ""

def _extract_confirmation_rules_text(d: dict, *, max_len: int | None = None) -> str:
    raw = d.get("confirmation_rules")
    text = ""
    if isinstance(raw, str):
        text = raw.strip()
    elif isinstance(raw, list):
        parts: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                parts.append(x.strip())
            elif isinstance(x, dict):
                txt = x.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        text = "; ".join(parts).strip()
    elif isinstance(raw, dict):
        txt = raw.get("text")
        if isinstance(txt, str):
            text = txt.strip()

    if not text:
        return ""
    if max_len is not None and max_len > 0 and len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text

def _wait_confirm_timeout_cap_for_signal(d: dict, *, default: int = 180) -> int:
    text = _extract_confirm_text(d).lower()
    if not text:
        return int(default)
    has_h1_context = any(token in text for token in (" h1", "h1 ", "1h", "час", "hour"))
    has_reclaim = any(
        token in text
        for token in (
            "reclaim",
            "возврат",
            "возвращается внутрь",
            "returns inside",
            "возврата выше",
            "возврата под",
        )
    )
    has_retest = any(token in text for token in ("retest", "ретест", "повторный тест", "повторного теста"))
    if has_reclaim or (has_h1_context and has_retest):
        return 360
    return int(default)


def _extract_max_wait_minutes(d: dict, default: int = 180) -> int:
    cap = _wait_confirm_timeout_cap_for_signal(d, default=default)
    for k in ("max_wait_minutes", "confirm_timeout_minutes", "max_valid_minutes", "validity_minutes", "max_valid_minutes"):
        v = _try_int(d.get(k))
        if isinstance(v, int) and v > 0:
            return min(int(v), cap)
    return min(int(default), cap)


def _append_unique_rule(rules: list[dict], rule: dict) -> None:
    if rule not in rules:
        rules.append(rule)


def _extract_event_window_minutes(text: str) -> int:
    numbers = [int(match) for match in re.findall(r"(\d{1,3})\s*мин", text)]
    bounded = [value for value in numbers if 5 <= value <= 240]
    if bounded:
        return max(bounded)
    return 90

def _resolve_signal_mode_from_last_json(d: dict, *, uid: int | None = None) -> str:
    if isinstance(uid, int):
        return get_user_mode(uid)

    raw_mode = d.get("mode")
    if isinstance(raw_mode, str):
        mode = raw_mode.strip().lower()
        if mode in VALID_MODES:
            return mode

    decision_path = d.get("decision_path")
    if isinstance(decision_path, list):
        first_mode = None
        for item in decision_path:
            if not isinstance(item, dict):
                continue
            raw = item.get("mode")
            if not isinstance(raw, str):
                continue
            mode = raw.strip().lower()
            if mode not in VALID_MODES:
                continue
            if first_mode is None:
                first_mode = mode
            result = str(item.get("result") or "").strip().lower()
            if result == "accepted":
                return mode
        if first_mode in VALID_MODES:
            return first_mode

    return "neutral"

def _confirm_profile_for_mode(mode: str | None) -> str:
    mode_key = str(mode or "").strip().lower()
    if mode_key == "aggressive":
        return "wait_confirm_light"
    if mode_key == "conservative":
        return "wait_confirm_structural"
    return "wait_confirm_standard"


def _append_mode_confirm_baseline_rules(rules: list[dict], *, profile: str, side: str) -> None:
    if profile == "wait_confirm_light":
        _append_unique_rule(rules, {"type": "close_in_entry_zone"})
        if side in ("long", "short"):
            _append_unique_rule(rules, {"type": "m15_impulse_in_direction", "side": side})
        return

    if profile == "wait_confirm_standard":
        _append_unique_rule(rules, {"type": "wick_into_entry_zone", "required": True})
        _append_unique_rule(rules, {"type": "close_in_entry_zone"})
        if side in ("long", "short"):
            _append_unique_rule(rules, {"type": "m15_impulse_in_direction", "side": side})
        return

    _append_unique_rule(rules, {"type": "close_in_entry_zone"})


def _build_confirm_rule_v1(d: dict, *, max_wait_minutes: int, mode: str | None = None) -> dict:
    rules: list[dict] = []
    raw = d.get("confirmation_rules")
    text = raw if isinstance(raw, str) else ""
    if isinstance(raw, list):
        text_parts: list[str] = []
        for x in raw:
            if isinstance(x, str):
                text_parts.append(x)
            elif isinstance(x, dict):
                t = x.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
        text = " ".join(text_parts)
    norm = str(text).lower()
    side = str(d.get("direction") or d.get("side") or "").strip().lower()
    is_long = side == "long"
    is_short = side == "short"
    mode_key = str(mode or _resolve_signal_mode_from_last_json(d)).strip().lower()
    profile = _confirm_profile_for_mode(mode_key)
    has_zone = any(token in norm for token in ("entry_range", "диапазон", "зон", "range", "границ"))
    has_hold = any(
        token in norm
        for token in (
            "удерж",
            "закреп",
            "hold",
            "holds",
            "удержания",
            "удерживает",
            "внутрь диапазона",
            "выше нижней границы",
            "ниже верхней границы",
        )
    )
    has_retest = any(
        token in norm
        for token in (
            "ретест",
            "retest",
            "после теста",
            "после касания",
            "после прокола",
            "после тестирования",
            "после тест",
        )
    )
    has_reclaim = any(
        token in norm
        for token in (
            "возврат",
            "reclaim",
            "returns inside",
            "возвращается внутрь",
            "быстрым возвратом",
            "возврата под",
            "возврат под",
            "возврата выше",
            "возврат выше",
            "под уровень",
        )
    )
    has_impulse = any(
        token in norm
        for token in (
            "импульс",
            "impulse",
            "отбой",
            "отскок",
            "bounce",
            "rejection",
            "подтверждение покупателя",
            "подтверждение продавца",
            "higher-lows",
            "lower-highs",
            "серия свеч",
            "серия удержаний",
        )
    )
    session_gate = any(
        token in norm
        for token in (
            "eu/us",
            "eu / us",
            "us-сесс",
            "us session",
            "europe/us",
            "предпочтительно подтверждение в eu/us",
            "лучше eu/us",
            "подтверждение в eu/us",
            "низкой ликвидности",
            "thin liquidity",
            "low-liquidity",
            "ази",
            "night",
        )
    )
    event_window_caution = any(
        token in norm
        for token in (
            "upcoming_events",
            "ppi",
            "fed",
            "fomc",
            "cpi",
            "headline",
            "новост",
            "событ",
            "реч",
            "до/после",
            "до и 90 минут после",
            "в пределах окна",
            "перед fed",
            "fed-speakers",
        )
    )

    _append_mode_confirm_baseline_rules(rules, profile=profile, side=side)

    if "m15" in norm and "ema20" in norm:
        explicit_above = "не ниже" in norm
        explicit_below = "не выше" in norm
        has_above = explicit_above or any(token in norm for token in (">=", "выше"))
        has_below = explicit_below or any(token in norm for token in ("<=", "ниже"))
        if has_above and not explicit_below and not (has_below and not explicit_above):
            _append_unique_rule(rules, {"type": "m15_close_vs_ema20", "op": "above"})
        elif has_below and not explicit_above and not (has_above and not explicit_below):
            _append_unique_rule(rules, {"type": "m15_close_vs_ema20", "op": "below"})

    if "объ" in norm and "средн" in norm and "20" in norm and "m15" in norm:
        _append_unique_rule(rules, {"type": "volume_m15_vs_avg20", "op": ">="})

    if "тенью" in norm and ("диапазон" in norm or "зон" in norm) and ("внутр" in norm or "зайти" in norm):
        _append_unique_rule(rules, {"type": "wick_into_entry_zone", "required": True})

    if has_zone and has_hold:
        _append_unique_rule(rules, {"type": "close_in_entry_zone"})

    if has_zone and has_retest:
        _append_unique_rule(rules, {"type": "retest_entry_zone", "required": profile != "wait_confirm_light"})

    if has_zone and has_reclaim and (is_long or is_short):
        _append_unique_rule(rules, {"type": "reclaim_entry_zone", "side": side})

    if has_impulse and (is_long or is_short):
        _append_unique_rule(rules, {"type": "m15_impulse_in_direction", "side": side})

    if session_gate:
        _append_unique_rule(rules, {"type": "session_gate", "allowed_sessions": ["eu", "us"]})

    if event_window_caution:
        _append_unique_rule(
            rules,
            {
                "type": "event_window_clear",
                "min_minutes": _extract_event_window_minutes(norm),
                "max_event_risk": "low",
            },
        )

    _append_unique_rule(rules, {"type": "deadline_minutes", "value": int(max_wait_minutes)})
    return {
        "version": 1,
        "profile": profile,
        "mode": mode_key if mode_key in VALID_MODES else "neutral",
        "policy": {
            "wait_confirm_light": {
                "zone_interaction": "optional",
                "min_directional_hits": 1,
            },
            "wait_confirm_standard": {
                "zone_interaction": "required",
                "min_directional_hits": 1,
                "min_structural_hits": 1,
            },
            "wait_confirm_structural": {
                "zone_interaction": "optional",
                "min_structural_hits": 1,
            },
        }.get(profile, {}),
        "rules": rules,
    }

def _build_signal_json_v1(
    *,
    signal_id: str,
    published_at: str,
    channel_id,
    origin_chat_id=None,
    publish_targets: list[int] | None = None,
    symbol_hint: str | None = None,
    last_payload: dict | None = None,
    last_json_path: Path | None = None,
) -> dict | None:
    if isinstance(last_payload, dict):
        d = last_payload
    else:
        try:
            d = _read_last_signal_json(path=last_json_path)
        except TypeError:
            d = _read_last_signal_json()
    if not isinstance(d, dict):
        return None

    symbol = d.get("symbol") or symbol_hint
    direction = d.get("direction") or d.get("side")
    entry_range_raw = d.get("entry_range")
    if isinstance(entry_range_raw, dict):
        entry_low = entry_range_raw.get("min")
        entry_high = entry_range_raw.get("max")
    elif isinstance(entry_range_raw, (list, tuple)) and len(entry_range_raw) == 2:
        entry_low = entry_range_raw[0]
        entry_high = entry_range_raw[1]
    else:
        entry_low = None
        entry_high = None
    sl = d.get("sl")
    tp1 = d.get("tp1")
    tp2 = d.get("tp2")
    tp3 = d.get("tp3")

    if not symbol or not direction:
        return None
    if entry_low is None or entry_high is None:
        return None

    try:
        entry_zone = [float(entry_low), float(entry_high)]
    except Exception:
        return None

    try:
        if isinstance(channel_id, str) and channel_id.strip().lstrip("-").isdigit():
            channel_id = int(channel_id.strip())
    except Exception:
        pass
    origin_user_id = None
    origin_chat_id = _normalize_chat_id(
        origin_chat_id if origin_chat_id is not None else d.get("origin_chat_id")
    )
    if origin_chat_id is None:
        origin_chat_id = _normalize_chat_id(channel_id)
    publish_targets = _normalize_chat_id_list(
        publish_targets if publish_targets is not None else d.get("publish_targets")
    )
    normalized_channel_id = _normalize_chat_id(channel_id)
    if not publish_targets and normalized_channel_id is not None:
        publish_targets = [normalized_channel_id]

    mode = "neutral"
    meta = None
    try:
        uid_val = d.get("uid")
        if uid_val is None:
            uid_val = d.get("user_id")
        if uid_val is None:
            uid_val = d.get("tg_uid")
        uid = None
        if isinstance(uid_val, int):
            uid = uid_val
        elif isinstance(uid_val, str) and uid_val.strip().isdigit():
            uid = int(uid_val.strip())
        if uid is None and isinstance(_AIA_UID_CONTEXT, int):
            uid = _AIA_UID_CONTEXT
        origin_user_id = uid

        mode = _resolve_signal_mode_from_last_json(d, uid=uid)

        raw_entry_mode = d.get("entry_mode")
        entry_type = None
        if raw_entry_mode is None:
            entry_type = "pullback"
        elif raw_entry_mode in ("wait_confirm", "confirm"):
            entry_type = "wait_confirm"
        elif raw_entry_mode == "pullback":
            entry_type = "pullback"
        elif raw_entry_mode == "breakout":
            entry_type = "breakout"
        else:
            entry_type = "pullback"

        market_phase = None
        if entry_type == "breakout":
            market_phase = "impulse"
        elif entry_type == "wait_confirm":
            market_phase = "range"
        elif entry_type == "pullback":
            market_phase = "consolidation"
        else:
            market_phase = "consolidation"

        if (
            mode in ("aggressive", "neutral", "conservative")
            and entry_type in ("pullback", "breakout", "wait_confirm")
            and market_phase in ("impulse", "consolidation", "range")
        ):
            meta = {"mode": mode, "entry_type": entry_type, "market_phase": market_phase}
    except Exception:
        meta = None

    if meta is not None:
        execution_diagnosis = d.get("execution_diagnosis")
        if isinstance(execution_diagnosis, dict):
            diag_out = {}
            items = execution_diagnosis.get("items")
            if isinstance(items, list) and "overextended_leader_repeat_long" in items:
                diag_out["overextended_leader_repeat_long"] = True
            elif execution_diagnosis.get("overextended_leader_repeat_long"):
                diag_out["overextended_leader_repeat_long"] = True
            if execution_diagnosis.get("requires_reset_reclaim"):
                diag_out["requires_reset_reclaim"] = True
            count = execution_diagnosis.get("same_asset_direction_recent_no_confirm_count")
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                diag_out["same_asset_direction_recent_no_confirm_count"] = int(count)
            if diag_out:
                meta["execution_diagnosis"] = diag_out

        try:
            rr_val = None
            rr_by_mode = d.get("rr_by_mode")
            if isinstance(rr_by_mode, dict):
                rr_m = rr_by_mode.get(mode)
                if isinstance(rr_m, (int, float)) and not isinstance(rr_m, bool):
                    rr_val = float(rr_m)
            if rr_val is None:
                rr = d.get("rr")
                if isinstance(rr, (int, float)) and not isinstance(rr, bool):
                    rr_val = float(rr)
            if rr_val is not None:
                rr_quality = None
                if rr_val >= 2.0:
                    rr_quality = "high"
                elif 1.3 <= rr_val < 2.0:
                    rr_quality = "medium"
                else:
                    rr_quality = "low"
                if rr_quality is not None:
                    meta["rr_quality"] = rr_quality
        except Exception:
            pass

        news_context = d.get("news_context")
        if isinstance(news_context, list):
            pos = 0
            neg = 0
            neu = 0
            for s in news_context:
                if not isinstance(s, str):
                    continue
                pos += s.count("[impact:+]")
                neg += s.count("[impact:-]")
                neu += s.count("[impact:neutral]")
            if pos > neg and pos >= 1:
                meta["news_bias"] = "positive"
            elif neg > pos and neg >= 1:
                meta["news_bias"] = "negative"
            elif pos == 0 and neg == 0 and neu >= 1:
                meta["news_bias"] = "neutral"
            else:
                meta["news_bias"] = "unknown"

    try:
        def _try_float(v):
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        entry_price = None
        try:
            entry_price = _try_float(d.get(f"entry_price_{mode}"))
        except Exception:
            entry_price = None
        if entry_price is None:
            low = _try_float(entry_zone[0] if len(entry_zone) == 2 else None)
            high = _try_float(entry_zone[1] if len(entry_zone) == 2 else None)
            if low is not None and high is not None:
                entry_price = (low + high) / 2.0
        if entry_price is None:
            ez = d.get("entry_zone")
            if isinstance(ez, (list, tuple)) and len(ez) == 2:
                low = _try_float(ez[0])
                high = _try_float(ez[1])
                if low is not None and high is not None:
                    entry_price = (low + high) / 2.0

        sl_val = None
        sl_by_mode = d.get("sl_by_mode")
        if isinstance(sl_by_mode, dict):
            sl_val = _try_float(sl_by_mode.get(mode))
        if sl_val is None:
            sl_val = float(sl)

        top_tp1_f = _try_float(tp1) if tp1 is not None else None
        top_tp2_f = _try_float(tp2) if tp2 is not None else None
        top_tp3_f = _try_float(tp3) if tp3 is not None else None
        tp_out: dict = {"tp1": None, "tp2": None}
        tp_by_mode = d.get("tp_by_mode")
        tp1_mode = None
        tp2_mode = None
        tp3_mode = None
        if isinstance(tp_by_mode, dict):
            tp_m = tp_by_mode.get(mode)
            if isinstance(tp_m, dict):
                tp1_mode = tp_m.get("tvh1")
                tp2_mode = tp_m.get("tvh2")
                tp3_mode = tp_m.get("tvh3") if tp_m.get("tvh3") is not None else tp_m.get("tp3")

        tp1_mode_f = _try_float(tp1_mode)
        tp2_mode_f = _try_float(tp2_mode)
        tp3_mode_f = _try_float(tp3_mode)
        if top_tp1_f is not None or top_tp2_f is not None or top_tp3_f is not None:
            tp_out = {"tp1": top_tp1_f, "tp2": top_tp2_f}
            if top_tp3_f is not None:
                tp_out["tp3"] = top_tp3_f
        elif tp1_mode_f is not None or tp2_mode_f is not None or tp3_mode_f is not None:
            tp_out = {"tp1": tp1_mode_f, "tp2": tp2_mode_f}
            if tp3_mode_f is not None:
                tp_out["tp3"] = tp3_mode_f
        else:
            tp_out = {"tp1": top_tp1_f, "tp2": top_tp2_f}
    except Exception:
        try:
            sl_val = float(sl)
            tp_out = {"tp1": (float(tp1) if tp1 is not None else None), "tp2": (float(tp2) if tp2 is not None else None)}
            if tp3 is not None:
                tp_out["tp3"] = float(tp3)
        except Exception:
            return None

    out = {
        "signal_id": str(signal_id),
        "signal_origin": "user",
        "symbol": str(symbol),
        "direction": str(direction),
        "entry_zone": entry_zone,
        "sl": sl_val,
        "tp": tp_out,
        "published_at": str(published_at),
        "channel_id": channel_id,
    }
    if origin_user_id is not None:
        out["origin_user_id"] = origin_user_id
    if origin_chat_id is not None:
        out["origin_chat_id"] = origin_chat_id
    if publish_targets:
        out["publish_targets"] = publish_targets
    if entry_price is not None:
        out["entry_price"] = float(entry_price)
    event_risk = _build_aia_event_risk_payload(d)
    if event_risk:
        out["event_risk"] = event_risk
    for key in (
        "macro_event_context",
        "macro_event_guard",
        "macro_event_diagnostics",
        "scheduled_macro_events",
        "upcoming_events",
    ):
        value = d.get(key)
        if value not in (None, "", [], {}):
            out[key] = copy.deepcopy(value)
    if meta is not None:
        out["meta"] = meta
        if meta.get("entry_type") == "wait_confirm":
            max_wait_minutes = _extract_max_wait_minutes(d, default=180)
            out["meta"]["max_wait_minutes"] = int(max_wait_minutes)
            out["meta"]["confirm_timeout_minutes"] = int(max_wait_minutes)
            out["meta"]["confirm_rule_v1"] = _build_confirm_rule_v1(d, max_wait_minutes=max_wait_minutes, mode=mode)
    return out

def send_signal_to_aia(signal_json_v1: dict) -> bool:
    try:
        status, _ = _aia_request_json("POST", "/tg/signal", body=signal_json_v1)
        return 200 <= status < 300
    except Exception:
        return False

def send_no_trade_decision_to_aia(payload: dict) -> bool:
    try:
        status, _ = _aia_request_json("POST", "/tg/decision", body=payload)
        return 200 <= status < 300
    except Exception:
        return False

def fetch_trade_report(signal_id: str) -> tuple[bool, str]:
    try:
        status, data = _aia_request_json("GET", "/jobs/trade_report", query={"signal_id": signal_id})
        if not (200 <= status < 300):
            return False, f"AIA: ошибка {status} при получении отчёта."
        if not isinstance(data, dict):
            return False, "AIA: неожиданный ответ (не JSON)."
        summary = data.get("summary")
        if not summary:
            return False, "AIA: отчёт пустой."
        return True, str(summary)
    except Exception:
        return False, "AIA недоступен. Попробуй позже."

def fetch_daily_summary(window_hours: int = 24) -> tuple[bool, str]:
    try:
        status, data = _aia_request_json("GET", "/jobs/daily_summary", query={"window_hours": int(window_hours)})
        if not (200 <= status < 300):
            return False, f"AIA: ошибка {status} при получении daily summary."
        if not isinstance(data, dict):
            return False, "AIA: неожиданный ответ (не JSON)."
        summary = data.get("summary")
        if not summary:
            return False, "AIA: summary пустой."
        return True, str(summary)
    except Exception:
        return False, "AIA недоступен. Попробуй позже."

async def _send_signal_to_aia_background(signal_json_v1: dict) -> None:
    try:
        await asyncio.to_thread(send_signal_to_aia, signal_json_v1)
    except Exception:
        pass

async def _send_no_trade_decision_to_aia_background(payload: dict) -> None:
    try:
        await asyncio.to_thread(send_no_trade_decision_to_aia, payload)
    except Exception:
        pass

def _queue_aia_signal_forward(
    signal_json_v1: dict,
    *,
    uid: int,
    symbol: str | None,
    mode: str,
    target_chat_id: int,
    source: str,
    signal_id: str,
    delivery_kind: str,
) -> None:
    _log_signal_publication(
        event="aia_forward_signal",
        uid=uid,
        symbol=symbol,
        mode=mode,
        target_chat_id=target_chat_id,
        source=source,
        signal_id=signal_id,
        delivery_kind=delivery_kind,
    )
    asyncio.create_task(_send_signal_to_aia_background(signal_json_v1))

def _queue_aia_no_trade_forward(
    payload: dict,
    *,
    uid: int,
    symbol: str | None,
    mode: str,
    target_chat_id: int,
    source: str,
    signal_id: str,
    delivery_kind: str,
) -> None:
    _log_signal_publication(
        event="aia_forward_no_trade",
        uid=uid,
        symbol=symbol,
        mode=mode,
        target_chat_id=target_chat_id,
        source=source,
        signal_id=signal_id,
        delivery_kind=delivery_kind,
    )
    asyncio.create_task(_send_no_trade_decision_to_aia_background(payload))

async def _publish_signal_result(
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    *,
    text: str,
    target_chat_id: int,
    delivery_kind: str,
    source: str,
    symbol_hint: str | None,
    sig_html: Path | None,
    run_log: Path | None,
    skip_aia_forward: bool = False,
) -> bool:
    published_at = _utc_now_z()
    signal_id = _infer_signal_id(sig_html, run_log, published_at)
    symbol = _resolve_signal_symbol(symbol_hint)
    mode = get_user_mode(uid)
    manual_state_guard = None
    if _should_evaluate_manual_state_guard(source, delivery_kind, text):
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        last_payload = _read_last_signal_json()
        candidate = _manual_state_guard_candidate_from_payload(
            last_payload,
            signal_id=signal_id,
            symbol_hint=symbol_hint,
            mode=mode,
            created_at=published_at,
        )
        manual_state_guard = _evaluate_manual_state_guard(candidate, now_utc=now_utc)
        manual_state_guard.update(_manual_state_guard_duplicate_runtime(manual_state_guard))
        enforcement_action = manual_state_guard["manual_state_guard_enforcement_action"]
        legacy_action = _manual_state_guard_legacy_wait_confirm_action(manual_state_guard)
        effective_action = enforcement_action if enforcement_action != "none" else legacy_action
        manual_state_guard["manual_duplicate_suppressed"] = effective_action in {
            "suppress_duplicate",
            "convert_to_management_update",
            "duplicate_wait_confirm_suppressed",
            "replace_wait_confirm",
        }
        _jsonl_append(
            _manual_state_guard_shadow_log_path(now_utc),
            _manual_state_guard_jsonl_row(
                now_utc=now_utc,
                request_type=source,
                candidate=candidate,
                result=manual_state_guard,
                enforcement_action=effective_action,
            ),
        )
        if effective_action in {
            "suppress_duplicate",
            "convert_to_management_update",
            "duplicate_wait_confirm_suppressed",
            "replace_wait_confirm",
        }:
            guard_text = render_state_guard_classification_message(
                {
                    **manual_state_guard,
                    "symbol": candidate.get("symbol") if isinstance(candidate, dict) else symbol,
                    "direction": candidate.get("direction") if isinstance(candidate, dict) else None,
                },
                symbol=symbol,
            )
            delivered_chat_ids = await _deliver_publication_targets(
                context,
                uid,
                guard_text,
                delivery_kind=delivery_kind,
                protect_content=False,
            )
            _log_signal_publication(
                event="telegram_publish_suppressed",
                uid=uid,
                symbol=symbol,
                mode=mode,
                target_chat_id=target_chat_id,
                source=source,
                signal_id=signal_id,
                delivery_kind=delivery_kind,
                manual_state_guard=manual_state_guard,
            )
            return bool(delivered_chat_ids)
        if (
            str(manual_state_guard.get("manual_state_guard_decision") or "").upper() == "RE_ENTRY_SIGNAL"
            and manual_state_guard.get("manual_state_guard_reentry_allowed") is True
        ):
            text = (
                render_state_guard_classification_message(
                    {
                        **manual_state_guard,
                        "symbol": candidate.get("symbol") if isinstance(candidate, dict) else symbol,
                        "direction": candidate.get("direction") if isinstance(candidate, dict) else None,
                    },
                    symbol=symbol,
                )
                + "\n\n"
                + text
            )
    delivered_chat_ids = await _deliver_publication_targets(
        context,
        uid,
        text,
        delivery_kind=delivery_kind,
        protect_content=False,
    )
    if not delivered_chat_ids:
        _log_signal_publication(
            event="telegram_publish_failed",
            uid=uid,
            symbol=symbol,
            mode=mode,
            target_chat_id=target_chat_id,
            source=source,
            signal_id=signal_id,
            delivery_kind=delivery_kind,
            manual_state_guard=manual_state_guard,
        )
        return False

    for delivered_chat_id in delivered_chat_ids:
        _log_signal_publication(
            event="telegram_publish",
            uid=uid,
            symbol=symbol,
            mode=mode,
            target_chat_id=delivered_chat_id,
            source=source,
            signal_id=signal_id,
            delivery_kind=delivery_kind,
            manual_state_guard=manual_state_guard,
        )

    if "📌 Сигнал не выдан" in text:
        payload = _build_no_trade_decision_payload(
            uid,
            tg_text=text,
            symbol_hint=symbol_hint,
            channel_id=target_chat_id,
            origin_chat_id=target_chat_id,
            publish_targets=delivered_chat_ids,
        )
        if payload and _should_send_to_aia_for_target(target_chat_id):
            _queue_aia_no_trade_forward(
                payload,
                uid=uid,
                symbol=symbol,
                mode=mode,
                target_chat_id=target_chat_id,
                source=source,
                signal_id=signal_id,
                delivery_kind=delivery_kind,
            )
        return True

    global _AIA_UID_CONTEXT
    _AIA_UID_CONTEXT = uid
    sig_v1 = _build_signal_json_v1(
        signal_id=signal_id,
        published_at=published_at,
        channel_id=target_chat_id,
        origin_chat_id=target_chat_id,
        publish_targets=delivered_chat_ids,
        symbol_hint=symbol_hint,
    )
    if sig_v1 and not skip_aia_forward and _should_send_to_aia_for_target(target_chat_id):
        _queue_aia_signal_forward(
            sig_v1,
            uid=uid,
            symbol=symbol,
            mode=mode,
            target_chat_id=target_chat_id,
            source=source,
            signal_id=signal_id,
            delivery_kind=delivery_kind,
        )
    return True

def _normalize_direction_v1(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("long", "short"):
        return s
    if s in ("buy", "bull", "bullish"):
        return "long"
    if s in ("sell", "bear", "bearish"):
        return "short"
    return None

def _extract_no_trade_reason_text(tg_text: str) -> str:
    s = (tg_text or "").strip()
    if not s:
        return ""

    low = s.lower()
    start = None
    for marker in ("причина (no trade):", "причина:"):
        i = low.find(marker)
        if i >= 0:
            start = i + len(marker)
            break
    if start is None:
        start = 0

    tail = s[start:].strip()
    lines = [ln.strip() for ln in tail.splitlines()]

    picked: list[str] = []
    for ln in lines:
        if not ln:
            continue
        lnl = ln.lower()
        if lnl.startswith("что должно измениться"):
            break
        if lnl.startswith("статус:"):
            break
        if ln.startswith("• "):
            ln = ln[2:].strip()
        if ln.startswith(("– ", "- ")):
            continue
        if ln and not ln.startswith("📌"):
            picked.append(ln)
        if len(picked) >= 2:
            break

    out = " ".join(picked).strip()
    if not out:
        return ""
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > 320:
        out = out[:320].rstrip()
    return out

def _build_no_trade_decision_payload(
    uid: int,
    *,
    tg_text: str,
    symbol_hint: str | None,
    channel_id: int,
    origin_chat_id: int | None = None,
    publish_targets: list[int] | None = None,
) -> dict | None:
    last = _read_last_signal_json() or {}
    asset = (symbol_hint or last.get("symbol") or last.get("asset"))
    if not asset:
        return None

    direction = _normalize_direction_v1(last.get("direction") or last.get("side"))
    if direction is None:
        return None

    mode = get_user_mode(uid)

    low = (tg_text or "").lower()
    if "risk" in low:
        reason_code = "risk_window"
        market_phase = "range"
        entry_type = "wait_confirm"
    elif "structural" in low:
        reason_code = "structural"
        market_phase = "consolidation"
        entry_type = "pullback"
    elif "liquidity" in low:
        reason_code = "liquidity"
        market_phase = "consolidation"
        entry_type = "pullback"
    else:
        reason_code = "rr_invalid"
        market_phase = "consolidation"
        entry_type = "pullback"

    reason_text = _extract_no_trade_reason_text(tg_text)
    if not reason_text:
        reason_text = "NO_TRADE"

    payload = {
        "channel_id": channel_id,
        "signal_origin": "user",
        "signal_candidate": {"asset": str(asset), "direction": direction, "mode": mode},
        "reason_code": reason_code,
        "reason_text": reason_text,
        "market_phase": market_phase,
        "entry_type": entry_type,
        "evaluated_at": _utc_now_z(),
    }
    payload["origin_user_id"] = uid
    normalized_origin = _normalize_chat_id(origin_chat_id)
    normalized_targets = _normalize_chat_id_list(publish_targets)
    if normalized_origin is not None:
        payload["origin_chat_id"] = normalized_origin
    if normalized_targets:
        payload["publish_targets"] = normalized_targets
    return payload

async def _is_chat_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    uid = update.effective_user.id
    chat = update.effective_chat
    if chat:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=uid)
            if getattr(member, "status", None) in ("creator", "administrator"):
                return True
        except Exception:
            pass
    return is_admin(uid)

async def _send_to_current_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat = update.effective_chat
    if chat:
        await context.bot.send_message(chat_id=chat.id, text=text)
        return
    if update.message:
        await update.message.reply_text(text)

def set_params_mode(mode: str):
    if mode not in VALID_MODES:
        mode = "neutral"

    params = None
    if PARAMS_PATH.exists():
        try:
            params = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        except Exception:
            params = None

    if not isinstance(params, dict):
        params = {"hints": {"mode": "neutral"}}

    hints = params.get("hints")
    if not isinstance(hints, dict):
        hints = {}
        params["hints"] = hints
    hints["mode"] = mode

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(PARAMS_PATH.parent),
            prefix=f"{PARAMS_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(params, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, PARAMS_PATH)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

# ============== SYMBOLS / MENU ==============

def load_symbols():
    try:
        with open(PROJECT_ROOT / "pool.json", "r", encoding="utf-8") as f:
            pool = json.load(f)["pool"]
        return [s.split("/")[0] for s in pool]
    except Exception:
        return FIXED_BOT_ASSETS.copy()

SYMBOLS     = load_symbols()
SYMBOLS_SET = set(SYMBOLS)

def _panel_symbols() -> list[str]:
    if PANEL_SYMBOLS_MAX <= 0:
        return []
    return SYMBOLS[:PANEL_SYMBOLS_MAX]

def panel_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("✅ Подписаться", callback_data="panel:subscribe")],
        [InlineKeyboardButton("💬 Открыть Signal bot", url=_signal_bot_link())],
        [
            InlineKeyboardButton("📈 Анализ", callback_data="panel:analysis"),
            InlineKeyboardButton("🤖 FULL", callback_data="panel:full"),
        ],
    ]
    symbols = _panel_symbols()
    for i in range(0, len(symbols), 3):
        rows.append([InlineKeyboardButton(x, callback_data=f"panel:sig:{x}") for x in symbols[i:i+3]])
    if len(symbols) < len(SYMBOLS):
        rows.append([InlineKeyboardButton("⋯ Другие", url=_signal_bot_link())])
    return InlineKeyboardMarkup(rows)

def main_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Сигнал")],
            [KeyboardButton("📈 Анализ")],
            [KeyboardButton("⚙️ Режим")],
            [KeyboardButton("📝 Регистрация")],
            [KeyboardButton("🎁 5 сигналов"), KeyboardButton("💳 Оплата")],
            [KeyboardButton("🧾 Статус"), KeyboardButton("🔗 Signal bot")],
        ],
        resize_keyboard=True
    )

def signal_menu_kb():
    rows = [
        [KeyboardButton("🤖 Auto (FULL)")],
    ]
    for i in range(0,len(SYMBOLS),3):
        rows.append([KeyboardButton(x) for x in SYMBOLS[i:i+3]])
    rows.append([KeyboardButton("⬅️ Назад")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def analysis_menu_kb(uid: int):
    rows = []
    for row in analysis_menu_layout(is_admin(uid)):
        rows.append([KeyboardButton(x) for x in row])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def mode_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🟥 Агрессивный")],
            [KeyboardButton("🟨 Нейтральный")],
            [KeyboardButton("🟩 Консервативный")],
            [KeyboardButton("⬅️ Назад")],
        ],
        resize_keyboard=True
    )

MODE_LABELS = {
    "aggressive": "Агрессивный",
    "neutral": "Нейтральный",
    "conservative": "Консервативный",
}

MODE_DESCRIPTIONS = {
    "aggressive": "🟥 Агрессивный — активный tactical режим: intraday / 1–2 дня, фокус на 1–2 TP.",
    "neutral": "🟨 Нейтральный — более аккуратный swing-tactical режим: 1–3 дня / short swing.",
    "conservative": "🟩 Консервативный — стратегический multi-day режим: 2–5 дней / отдельное удержание.",
}

# ============== RATE / LOCK =================

LAST_REQUEST_TS={}
GEN_LOCKED=False
GEN_LOCKED_TS=None

def can_request(uid:int):
    interval = get_min_interval(uid)
    if interval <= 0:
        return True, 0
    now = time.time()
    prev = LAST_REQUEST_TS.get(uid)
    if prev is None:
        return True, 0
    dt = now - prev
    if dt >= interval:
        return True, 0
    return False, int(interval - dt)

def touch_request(uid:int):
    LAST_REQUEST_TS[uid]=time.time()

def is_gen_locked():
    global GEN_LOCKED, GEN_LOCKED_TS
    if not GEN_LOCKED:
        return False
    timeout = int(GLOBAL_SETTINGS.get("lock_timeout_sec",120))
    if GEN_LOCKED_TS and time.time()-GEN_LOCKED_TS > timeout:
        GEN_LOCKED=False
        GEN_LOCKED_TS=None
        return False
    return True

def acquire_gen_lock():
    global GEN_LOCKED, GEN_LOCKED_TS
    if is_gen_locked():
        return False
    GEN_LOCKED=True
    GEN_LOCKED_TS=time.time()
    return True

def release_gen_lock():
    global GEN_LOCKED, GEN_LOCKED_TS
    GEN_LOCKED=False
    GEN_LOCKED_TS=None

# ============== HANDLERS ====================

async def whoami(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = get_user_cfg(uid)
    ch  = get_main_chat_id(uid)
    reg = get_user(uid)
    await update.message.reply_text(
        f"whoami\nuid: {uid}\nallowed(env): {ALLOWED_UIDS}\n"
        f"in JSON: {bool(cfg)}\nchannel: {ch}\nmode: {get_user_mode(uid)}\n"
        f"{status_text(reg)}"
    )

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and getattr(chat, "type", None) != "private":
        await _send_to_current_chat(update, context, "Открой бота в личном чате для меню и управления.")
        return
    uid = update.effective_user.id
    first = (update.effective_user.first_name or "").strip()
    record, created = register_user(update.effective_user)
    await update.message.reply_text(f"Привет, {first or 'трейдер'}!")
    if created:
        await update.message.reply_text("✅ Регистрация выполнена.")
    await update.message.reply_text(status_text(record))
    await update.message.reply_text(
        "Для доступа активируй trial или оплату.\n"
        "Trial: /trial\n"
        "Оплата (мок): /pay"
    )
    if is_allowed(uid):
        await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())

async def subscribe(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await handle_pay(update, context)

async def unsubscribe(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        set_status(uid, "registered")
        await update.message.reply_text("❌ Подписка отключена. Вернуться: /pay или /trial")
    except Exception:
        await update.message.reply_text("ℹ️ Подписка не найдена. Вернуться: /pay или /trial")

async def handle_register(update:Update, context:ContextTypes.DEFAULT_TYPE):
    record, created = register_user(update.effective_user)
    if created:
        await update.message.reply_text("✅ Регистрация выполнена.")
    else:
        await update.message.reply_text("✅ Регистрация обновлена.")
    await update.message.reply_text(status_text(record))

async def handle_trial(update:Update, context:ContextTypes.DEFAULT_TYPE):
    record = start_trial(update.effective_user)
    await update.message.reply_text("🎁 Trial активирован: 5 сигналов бесплатно.")
    await update.message.reply_text(status_text(record))

async def handle_pay(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        add_paid_allowed_uid(uid)
    except Exception:
        pass
    PAID_ALLOWED_UIDS.add(uid)
    record = set_paid(update.effective_user)
    await update.message.reply_text("💳 Оплата принята (мок). Подписка активирована.")
    await update.message.reply_text(status_text(record))

async def handle_status(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    record = get_user(uid)
    if not isinstance(record, dict):
        await update.message.reply_text("Сначала зарегистрируйся: /register")
        return
    await update.message.reply_text(status_text(record))

async def handle_signal_bot_info(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await _send_signal_bot_hint(update)

async def handle_panel_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_chat_admin_or_owner(update, context):
        await _send_to_current_chat(update, context, "⛔ Недоступно")
        return
    text = (
        "✅ Подписка и персональные сигналы\n"
        "1) Нажми «Подписаться»\n"
        "2) Открой Signal bot и нажми /start\n"
        "3) Запрашивай сигналы кнопками ниже — ответы придут в личку"
    )
    channels: list[int] = []
    if context.args:
        raw = str(context.args[0]).strip()
        if raw.lstrip("-").isdigit():
            channels = [int(raw)]
    if not channels and FALLBACK_CHANNEL:
        raw = str(FALLBACK_CHANNEL).strip()
        if raw.lstrip("-").isdigit():
            channels = [int(raw)]
    if not channels:
        channels = get_all_main_channels()
    if not channels:
        await _send_to_current_chat(update, context, "Каналы не найдены в конфиге.")
        return
    sent = 0
    for ch in channels:
        try:
            await context.bot.send_message(chat_id=ch, text=text, reply_markup=panel_kb())
            sent += 1
        except Exception:
            continue
    await _send_to_current_chat(update, context, f"Панель отправлена: {sent}")

async def handle_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = (query.data or "").strip()
    if not data.startswith("panel:"):
        return
    uid = query.from_user.id if query.from_user else None
    if uid is None:
        await query.answer()
        return

    if data == "panel:subscribe":
        try:
            add_paid_allowed_uid(uid)
            PAID_ALLOWED_UIDS.add(uid)
        except Exception:
            pass
        set_paid(query.from_user)
        ok = _send_personal(uid, "✅ Подписка активирована. Запрашивай сигналы в основном канале.", protect_content=False)
        if ok:
            await query.answer("Подписка активирована. Проверь личный чат Signal bot.", show_alert=True)
        else:
            await query.answer("Подписка активирована. Открой Signal bot и нажми /start.", show_alert=True)
        return

    if not is_allowed(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return

    record = get_user(uid)
    if not isinstance(record, dict):
        record, _ = register_user(query.from_user)
    if not is_active(record):
        await query.answer("Нет активной подписки. Нажми «Подписаться».", show_alert=True)
        return
    if not SIGNAL_BOT_TOKEN:
        await query.answer("Signal bot не настроен.", show_alert=True)
        return

    ok, wait = can_request(uid)
    if not ok:
        await query.answer(f"Подожди {wait} сек.", show_alert=True)
        return
    if is_gen_locked():
        await query.answer("Уже считаю сигнал, попробуй позже.", show_alert=True)
        return

    if data == "panel:analysis":
        if not _send_personal(uid, "Готовлю анализ… ⏳", protect_content=False):
            await query.answer("Открой Signal bot и нажми /start.", show_alert=True)
            return
        await query.answer("Готовлю… отправлю в личку.", show_alert=False)
        touch_request(uid)
        if not acquire_gen_lock():
            _send_personal(uid, "Занято, попробуй позже.", protect_content=False)
            return
        try:
            await _run_analysis_core(uid, context, record, status_msg=None, delivery_kind="personal")
        finally:
            release_gen_lock()
        return

    if data == "panel:full":
        if not _send_personal(uid, "Готовлю FULL… ⏳", protect_content=False):
            await query.answer("Открой Signal bot и нажми /start.", show_alert=True)
            return
        await query.answer("Готовлю… отправлю в личку.", show_alert=False)
        touch_request(uid)
        if not acquire_gen_lock():
            _send_personal(uid, "Занято, попробуй позже.", protect_content=False)
            return
        try:
            await _run_full_core(uid, context, record, status_msg=None, delivery_kind="personal")
        finally:
            release_gen_lock()
        return

    if data.startswith("panel:sig:"):
        sym = data.split("panel:sig:", 1)[1].strip().upper()
        if not sym or sym not in SYMBOLS_SET:
            await query.answer("Неизвестный тикер.", show_alert=True)
            return
        symbol = f"{sym}/USDT"
        if not _send_personal(uid, f"Готовлю сигнал по {symbol}… ⏳", protect_content=False):
            await query.answer("Открой Signal bot и нажми /start.", show_alert=True)
            return
        await query.answer("Готовлю… отправлю в личку.", show_alert=False)
        touch_request(uid)
        if not acquire_gen_lock():
            _send_personal(uid, "Занято, попробуй позже.", protect_content=False)
            return
        try:
            await _run_symbol_core(uid, symbol, context, record, status_msg=None, delivery_kind="personal")
        finally:
            release_gen_lock()
        return

    await query.answer()

async def _ensure_active_access(update: Update) -> dict | None:
    uid = update.effective_user.id
    record = get_user(uid)
    if not isinstance(record, dict):
        await update.message.reply_text("Сначала зарегистрируйся: /register")
        return None
    if not is_active(record):
        status = record.get("status")
        if status == "trial":
            await update.message.reply_text("Trial закончился. Продли: /pay")
        else:
            await update.message.reply_text("Нужна активация доступа: /trial или /pay")
        return None
    if get_main_publication_chat_id(uid) is None:
        await update.message.reply_text("Канал для публикации не настроен. Обратись к администратору.")
        return None
    return record

async def handle_back(update,context):
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())

async def handle_signal_menu(update,context):
    await update.message.reply_text("Выбери актив или режим:", reply_markup=signal_menu_kb())

async def handle_analysis_menu(update,context):
    uid = update.effective_user.id
    await update.message.reply_text("Выбери тип анализа:", reply_markup=analysis_menu_kb(uid))

async def handle_mode_menu(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    mode = get_user_mode(uid)
    current_line = MODE_DESCRIPTIONS.get(mode, MODE_DESCRIPTIONS["neutral"]).split(" — ")[0]
    desc = "\n".join((
        MODE_DESCRIPTIONS["aggressive"],
        MODE_DESCRIPTIONS["neutral"],
        MODE_DESCRIPTIONS["conservative"],
    ))
    await update.message.reply_text(
        f"Текущий режим: {current_line}\n\nРежимы:\n{desc}",
        reply_markup=mode_menu_kb()
    )

async def _handle_mode_choice(update, context, mode_key:str):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    set_user_mode(uid, mode_key)
    desc = MODE_DESCRIPTIONS.get(mode_key, MODE_DESCRIPTIONS["neutral"])
    if " — " in desc:
        _, tail = desc.split(" — ", 1)
    else:
        tail = desc
    await update.message.reply_text(
        f"Режим установлен: {MODE_LABELS.get(mode_key,'Нейтральный')} — {tail}",
        reply_markup=mode_menu_kb()
    )

async def handle_mode_aggressive(update,context):
    await _handle_mode_choice(update, context, "aggressive")

async def handle_mode_neutral(update,context):
    await _handle_mode_choice(update, context, "neutral")

async def handle_mode_conservative(update,context):
    await _handle_mode_choice(update, context, "conservative")

# ---------- INTERNAL RUNNERS ----------
async def _run_full_core(
    uid: int,
    context: ContextTypes.DEFAULT_TYPE,
    record: dict,
    *,
    status_msg=None,
    delivery_kind: str = "main",
) -> None:
    target_chat_id = _get_publication_target(uid, delivery_kind)
    if target_chat_id is None:
        if status_msg is not None:
            await status_msg.edit_text("Канал для публикации не настроен.")
        return
    set_params_mode(get_user_mode(uid))
    proc = subprocess.run(
        ["bash","-lc", f"cd '{PROJECT_ROOT}' && SIGNAL_SKIP_AIA_SEND=1 ./signal full"],
        capture_output=True, text=True, timeout=900
    )

    analysis = latest("analysis_*.md")
    sig_html, run_log = _resolve_signal_run_artifacts(proc)

    if sig_html:
        parts = html_file_to_tg_text(Path(sig_html))
        if parts:
            part0 = parts[0]
            await _publish_signal_result(
                context,
                uid,
                text=part0,
                target_chat_id=target_chat_id,
                delivery_kind=delivery_kind,
                source="tg_bot.py:_run_full_core",
                symbol_hint=None,
                sig_html=Path(sig_html) if sig_html else None,
                run_log=Path(run_log) if run_log else None,
            )
    elif status_msg is not None:
        await status_msg.edit_text("Не удалось сформировать publish HTML для текущего прогона.")
        return

    if record.get("status") == "trial":
        consume_trial(uid)

    if status_msg is not None:
        mode_label = MODE_LABELS.get(get_user_mode(uid), MODE_LABELS["neutral"])
        await status_msg.edit_text(
            format_done(
                f"✅ FULL: режим {mode_label}",
                logs_path=_relpath(run_log) if run_log else None,
            ),
            parse_mode=ParseMode.HTML
        )

async def _run_analysis_core(
    uid: int,
    context: ContextTypes.DEFAULT_TYPE,
    record: dict,
    *,
    status_msg=None,
    delivery_kind: str = "main",
) -> None:
    target_chat_id = _get_publication_target(uid, delivery_kind)
    if target_chat_id is None:
        if status_msg is not None:
            await status_msg.edit_text("Канал для публикации не настроен.")
        return
    set_params_mode(get_user_mode(uid))
    proc = subprocess.run(
        ["bash","-lc", f"cd '{PROJECT_ROOT}' && SIGNAL_SKIP_AIA_SEND=1 ./signal full"],
        capture_output=True, text=True, timeout=900
    )
    analysis = latest("analysis_*.md")
    _, run_log = _resolve_signal_run_artifacts(proc)
    if not analysis:
        if status_msg is not None:
            await status_msg.edit_text("Не удалось сформировать анализ.")
        return

    hdr = make_header("📝 LLM Анализ")
    txt_raw = Path(analysis).read_text(encoding="utf-8")
    txt = strip_snapshot(txt_raw).split("2️⃣ Сетап")[0].strip()
    await _deliver_publication(
        context,
        uid,
        hdr+"\n\n"+txt,
        delivery_kind=delivery_kind,
        protect_content=False,
    )

    if status_msg is not None:
        await status_msg.edit_text(
            format_done(
                f"✅ CURRENT: {_relpath(Path(analysis))}",
                logs_path=_relpath(run_log) if run_log else None,
            ),
            parse_mode=ParseMode.HTML
        )

async def _run_symbol_core(
    uid: int,
    symbol: str,
    context: ContextTypes.DEFAULT_TYPE,
    record: dict,
    *,
    status_msg=None,
    delivery_kind: str = "main",
) -> None:
    target_chat_id = _get_publication_target(uid, delivery_kind)
    if target_chat_id is None:
        if status_msg is not None:
            await status_msg.edit_text("Канал для публикации не настроен.")
        return
    set_params_mode(get_user_mode(uid))
    proc = subprocess.run(
        ["bash","-lc", f"cd '{PROJECT_ROOT}' && ./signal '{symbol}'"],
        capture_output=True, text=True, timeout=900
    )

    analysis = latest("analysis_*.md")
    sig_html, run_log = _resolve_signal_run_artifacts(proc)

    if sig_html:
        parts = html_file_to_tg_text(Path(sig_html))
        if parts:
            part0 = parts[0]
            await _publish_signal_result(
                context,
                uid,
                text=part0,
                target_chat_id=target_chat_id,
                delivery_kind=delivery_kind,
                source="tg_bot.py:_run_symbol_core",
                symbol_hint=symbol,
                sig_html=Path(sig_html) if sig_html else None,
                run_log=Path(run_log) if run_log else None,
            )
    elif status_msg is not None:
        await status_msg.edit_text("Не удалось сформировать publish HTML для текущего прогона.")
        return

    if record.get("status") == "trial":
        consume_trial(uid)

    if status_msg is not None:
        await status_msg.edit_text(
            format_done(
                f"✅ SIGNAL: {symbol}",
                logs_path=_relpath(run_log) if run_log else None,
            ),
            parse_mode=ParseMode.HTML
        )

# ---------- FULL ----------
async def handle_full(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    record = await _ensure_active_access(update)
    if record is None:
        return

    ok, wait = can_request(uid)
    if not ok:
        await update.message.reply_text(f"Подожди {wait} сек.")
        return

    if is_gen_locked():
        await update.message.reply_text("Уже считаю сигнал, попробуй позже.")
        return

    touch_request(uid)
    msg = await update.message.reply_text("Готовлю FULL… ⏳")

    if not acquire_gen_lock():
        await msg.edit_text("Занято, попробуй позже.")
        return

    try:
        await _run_full_core(uid, context, record, status_msg=msg)
    finally:
        release_gen_lock()

# ---------- ANALYSIS ----------
async def handle_current_analysis(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    record = await _ensure_active_access(update)
    if record is None:
        return

    ok, wait = can_request(uid)
    if not ok:
        await update.message.reply_text(f"Подожди {wait} сек.")
        return

    if is_gen_locked():
        await update.message.reply_text("Уже считается, попробуй позже.")
        return

    touch_request(uid)
    msg = await update.message.reply_text("Готовлю анализ… ⏳")

    if not acquire_gen_lock():
        await msg.edit_text("Занято, позже.")
        return

    try:
        await _run_analysis_core(uid, context, record, status_msg=msg)
    finally:
        release_gen_lock()

async def handle_analysis(update,context):
    await handle_current_analysis(update,context)

# ---------- SINGLE ----------
async def handle_symbol(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    record = await _ensure_active_access(update)
    if record is None:
        return

    ok, wait = can_request(uid)
    if not ok:
        await update.message.reply_text(f"Подожди {wait} сек.")
        return

    sym = (update.message.text or "").strip().upper()
    if sym not in SYMBOLS_SET:
        await update.message.reply_text("Не распознал тикер, выбери из меню.")
        return

    symbol = f"{sym}/USDT"
    msg = await update.message.reply_text(f"Готовлю сигнал по {symbol}… ⏳")

    if is_gen_locked():
        await msg.edit_text("Уже считается другой сигнал, попробуй позже.")
        return

    touch_request(uid)

    if not acquire_gen_lock():
        await msg.edit_text("Уже в работе, позже.")
        return

    try:
        await _run_symbol_core(uid, symbol, context, record, status_msg=msg)
    finally:
        release_gen_lock()

# ---------- DAY / MID ----------
async def _post_report(root_dir, emoji, context, channel, report_dir: Path) -> None:
    d = report_dir
    header_line = f"<b><u>{emoji} {root_dir.upper()} REPORT</u></b>\n"
    header_msg = await context.bot.send_message(chat_id=channel, text=header_line, parse_mode=ParseMode.HTML)
    an = sorted(d.glob("analysis_*.md"))
    report_msg = None
    if an:
        txt = an[-1].read_text(encoding="utf-8").strip()
        messages = _build_report_telegram_messages(root_dir, emoji, txt)
        for idx, message in enumerate(messages):
            sent = await context.bot.send_message(chat_id=channel, text=message)
            if idx == 0:
                report_msg = sent

    pinned_kind = root_dir.lower()
    if pinned_kind in ("day", "mid"):
        state = load_pinned_state(PINNED_STATE_PATH)
        prev_id = get_pinned_message_id(state, channel, pinned_kind)
        new_id = (report_msg.message_id if report_msg else header_msg.message_id)
        if prev_id and prev_id != new_id:
            try:
                await context.bot.unpin_chat_message(chat_id=channel, message_id=prev_id)
            except Exception:
                pass
        try:
            await context.bot.pin_chat_message(chat_id=channel, message_id=new_id, disable_notification=True)
        except Exception:
            pass
        try:
            save_pinned_state(PINNED_STATE_PATH, set_pinned_message_id(state, channel, pinned_kind, new_id))
        except Exception:
            pass
    return None

async def handle_day(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    if not is_admin(uid):
        await update.message.reply_text("⛔ Недоступно")
        return
    if is_gen_locked():
        await update.message.reply_text("Уже считается, позже.")
        return
    msg = await update.message.reply_text("Готовлю DAY… ⏳")
    if not acquire_gen_lock():
        await msg.edit_text("Занято, позже.")
        return
    try:
        proc = subprocess.run(
            ["bash","-lc", f"cd '{PROJECT_ROOT}' && chmod +x run_day.sh && ./run_day.sh"],
            capture_output=True, text=True, timeout=1200
        )
        report_dir = latest_report_dir("day")
        if not report_dir:
            await msg.edit_text("Отчёт не найден.")
            return
        for channel in get_all_main_channels():
            await _post_report("day","🗓",context,channel,report_dir=report_dir)
        report_rel = _relpath(report_dir)
        await msg.edit_text(
            format_done(
                f"✅ DAY report: {report_rel}",
                logs_path=None,
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        release_gen_lock()

async def handle_mid(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    if not is_admin(uid):
        await update.message.reply_text("⛔ Недоступно")
        return
    if is_gen_locked():
        await update.message.reply_text("Уже считается, позже.")
        return
    msg = await update.message.reply_text("Готовлю MID… ⏳")
    if not acquire_gen_lock():
        await msg.edit_text("Занято, позже.")
        return
    try:
        proc = subprocess.run(
            ["bash","-lc", f"cd '{PROJECT_ROOT}' && chmod +x run_mid.sh && ./run_mid.sh"],
            capture_output=True, text=True, timeout=1200
        )
        report_dir = latest_report_dir("mid")
        if not report_dir:
            await msg.edit_text("Отчёт не найден.")
            return
        for channel in get_all_main_channels():
            await _post_report("mid","📰",context,channel,report_dir=report_dir)
        report_rel = _relpath(report_dir)
        await msg.edit_text(
            format_done(
                f"✅ MID report: {report_rel}",
                logs_path=None,
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        release_gen_lock()

# ---------- AIA REPORTS ----------
async def handle_aia_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_chat_admin_or_owner(update, context):
        await _send_to_current_chat(update, context, "⛔ Недоступно")
        return
    if not context.args:
        await _send_to_current_chat(update, context, "Использование: /aia_report <signal_id>")
        return
    signal_id = (context.args[0] or "").strip()
    if not signal_id:
        await _send_to_current_chat(update, context, "Использование: /aia_report <signal_id>")
        return

    ok, summary = await asyncio.to_thread(fetch_trade_report, signal_id)
    if ok:
        await _send_to_current_chat(update, context, "Отчёт агента\n\n" + summary)
    else:
        await _send_to_current_chat(update, context, summary)

async def handle_aia_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_chat_admin_or_owner(update, context):
        await _send_to_current_chat(update, context, "⛔ Недоступно")
        return
    ok, summary = await asyncio.to_thread(fetch_daily_summary, 24)
    await _send_to_current_chat(update, context, summary)

# ============== REGISTER / MAIN ==============

def register_text_handlers(app:Application):
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📊 Сигнал$"), handle_signal_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📈 Анализ$"), handle_analysis_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📈 Current$"), handle_current_analysis))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⚙️ Режим$"), handle_mode_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📝 Регистрация$"), handle_register))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🎁 5 сигналов$"), handle_trial))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^💳 Оплата$"), handle_pay))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🧾 Статус$"), handle_status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🔗 Signal bot$"), handle_signal_bot_info))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🟥 Агрессивный$"), handle_mode_aggressive))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🟨 Нейтральный$"), handle_mode_neutral))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🟩 Консервативный$"), handle_mode_conservative))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🤖 Auto \\(FULL\\)$"), handle_full))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🗓 DAY$"), handle_day))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📰 MID$"), handle_mid))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⬅️ Назад$"), handle_back))

    sym_regex = "^(" + "|".join(sorted(SYMBOLS_SET)) + ")$"
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(sym_regex), handle_symbol))

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env.tg.clean")
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", handle_register))
    app.add_handler(CommandHandler("trial", handle_trial))
    app.add_handler(CommandHandler("pay", handle_pay))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("signalbot", handle_signal_bot_info))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("aia_report", handle_aia_report))
    app.add_handler(CommandHandler("aia_daily", handle_aia_daily))
    app.add_handler(CommandHandler("panel", handle_panel_publish))
    app.add_handler(CallbackQueryHandler(handle_panel_callback, pattern="^panel:"))
    register_text_handlers(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
