#!/usr/bin/env python3
from zoneinfo import ZoneInfo
import asyncio
import os, json, time, subprocess, re, html as htmllib, tempfile
from datetime import datetime
from pathlib import Path
from urllib import request as urlrequest, parse as urlparse

MSK = ZoneInfo("Europe/Moscow")

from dotenv import load_dotenv
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from cleanup_old_signals import cleanup_old_signals
from pinned_state import get_pinned_message_id, load_pinned_state, save_pinned_state, set_pinned_message_id
from rbac import analysis_menu_layout, is_admin

# ============== BASE / ENV ==================

BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE
FIXED_BOT_ASSETS = ["BTC", "ETH", "BNB", "SOL", "XRP"]

load_dotenv(BASE / ".env.tg.clean")

BOT_TOKEN = os.getenv("TELEGRAM_PERSONAL_BOT_TOKEN")
FALLBACK_CHANNEL = os.getenv("TELEGRAM_TARGET_CHANNEL")

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
PERSONAL_POLL_SEC = _parse_env_int(os.getenv("TELEGRAM_PERSONAL_POLL_SEC")) or 10
SIGNAL_ROOT = Path(os.getenv("TELEGRAM_SIGNAL_ROOT", PROJECT_ROOT))
SIGNAL_CLEANUP_DAYS = _parse_env_int(os.getenv("TELEGRAM_SIGNAL_CLEANUP_DAYS")) or 30
SIGNAL_CLEANUP_SEC = _parse_env_int(os.getenv("TELEGRAM_SIGNAL_CLEANUP_SEC")) or 21600

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

USER_CHANNELS_PATH = PROJECT_ROOT / "user_channels.json"
SUBSCRIPTIONS_PATH = Path(
    os.getenv("TELEGRAM_SUBSCRIPTIONS_PATH", PROJECT_ROOT / "user_subscriptions.json")
)
SUBSCRIPTIONS_LOG_PATH = Path(
    os.getenv(
        "TELEGRAM_SUBSCRIPTIONS_LOG_PATH",
        PROJECT_ROOT / "user_subscriptions.log.jsonl",
    )
)
PERSONAL_SIGNAL_STATE_PATH = Path(
    os.getenv(
        "TELEGRAM_PERSONAL_SIGNAL_STATE_PATH",
        PROJECT_ROOT / "personal_signal_state.json",
    )
)
PARAMS_PATH = PROJECT_ROOT / "params.json"
PINNED_STATE_PATH = PROJECT_ROOT / "pinned_state.json"
SHARED_MAIN_ROUTE_KEY = "shared_v3_mixed"
USER_CONFIG = {}
GLOBAL_SETTINGS = {"lock_timeout_sec": 120}
VALID_MODES = {"aggressive", "neutral", "conservative"}
_AIA_UID_CONTEXT = None
SUBSCRIBERS: set[int] = set()
USER_SIGNAL_PREFS: dict[str, dict] = {}

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
    prefs = data.get("prefs")
    if isinstance(prefs, dict):
        global USER_SIGNAL_PREFS
        USER_SIGNAL_PREFS = prefs

def _save_subscriptions() -> None:
    data = {"subscribers": sorted(SUBSCRIBERS), "prefs": USER_SIGNAL_PREFS}
    SUBSCRIPTIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _log_subscription_event(action: str, user) -> None:
    if user is None:
        return
    record = {
        "ts": time.time(),
        "ts_iso": _utc_now_z(),
        "action": action,
        "uid": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    with SUBSCRIPTIONS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def _load_personal_signal_state() -> dict:
    if not PERSONAL_SIGNAL_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(PERSONAL_SIGNAL_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_personal_signal_state(state: dict) -> None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(PERSONAL_SIGNAL_STATE_PATH.parent),
            prefix=f"{PERSONAL_SIGNAL_STATE_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, PERSONAL_SIGNAL_STATE_PATH)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

async def _broadcast_signal_to_subscribers(app: Application, sig_html: Path) -> None:
    parts = html_file_to_tg_text(sig_html)
    if not parts:
        return
    signal_info = _read_last_signal_json_from_root(SIGNAL_ROOT) or {}
    direction = _normalize_direction_v1(signal_info.get("direction") or signal_info.get("side"))
    signal_mode = signal_info.get("mode")
    subscribers = list(SUBSCRIBERS)
    for chat_id in subscribers:
        prefs = _get_user_prefs(chat_id)
        pref_dir = prefs.get("direction") or "all"
        pref_mode = prefs.get("mode") or "all"
        if pref_dir != "all" and direction and pref_dir != direction:
            continue
        if pref_mode != "all" and signal_mode and pref_mode != signal_mode:
            continue
        for idx, part in enumerate(parts):
            try:
                await app.bot.send_message(chat_id=chat_id, text=part)
            except Exception:
                continue

async def _watch_new_signals(app: Application) -> None:
    state = _load_personal_signal_state()
    last_path = state.get("path")
    last_mtime = state.get("mtime")
    initialized = bool(last_path)

    while True:
        try:
            sig_html = latest_in_root(SIGNAL_ROOT, "signal_*.html")
            if not initialized:
                if sig_html:
                    last_path = sig_html.as_posix()
                    last_mtime = sig_html.stat().st_mtime
                    _save_personal_signal_state(
                        {"path": last_path, "mtime": last_mtime}
                    )
                    initialized = True
                await asyncio.sleep(PERSONAL_POLL_SEC)
                continue

            if sig_html:
                current_path = sig_html.as_posix()
                current_mtime = sig_html.stat().st_mtime
                if current_path != last_path or (
                    last_mtime is not None and current_mtime > last_mtime
                ):
                    await _broadcast_signal_to_subscribers(app, sig_html)
                    last_path = current_path
                    last_mtime = current_mtime
                    _save_personal_signal_state(
                        {"path": last_path, "mtime": last_mtime}
                    )
        except Exception:
            pass

        await asyncio.sleep(PERSONAL_POLL_SEC)

async def _cleanup_old_signals_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(
                cleanup_old_signals,
                SIGNAL_ROOT,
                days=SIGNAL_CLEANUP_DAYS,
            )
        except Exception:
            pass
        await asyncio.sleep(SIGNAL_CLEANUP_SEC)

async def _post_init(app: Application) -> None:
    asyncio.create_task(_watch_new_signals(app))
    asyncio.create_task(_cleanup_old_signals_loop())

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
    return sorted(SUBSCRIBERS)

load_subscriptions()

def get_user_cfg(uid:int):
    return USER_CONFIG.get(str(uid))

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

def get_main_chat_id(uid:int):
    return uid

def get_main_publication_targets(uid: int) -> list[int]:
    primary = get_main_publication_chat_id(uid)
    if primary is None:
        return []
    return [primary]

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
    return True

def _load_user_signal_prefs() -> None:
    global USER_SIGNAL_PREFS
    USER_SIGNAL_PREFS = {}
    path = SUBSCRIPTIONS_PATH
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    prefs = data.get("prefs")
    if isinstance(prefs, dict):
        USER_SIGNAL_PREFS = prefs

def _save_user_signal_prefs() -> None:
    data = {}
    if SUBSCRIPTIONS_PATH.exists():
        try:
            data = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["subscribers"] = sorted(SUBSCRIBERS)
    data["prefs"] = USER_SIGNAL_PREFS
    SUBSCRIPTIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _get_user_prefs(uid: int) -> dict:
    raw = USER_SIGNAL_PREFS.get(str(uid))
    if isinstance(raw, dict):
        return raw
    return {}

def _set_user_prefs(uid: int, prefs: dict) -> None:
    USER_SIGNAL_PREFS[str(uid)] = prefs
    _save_user_signal_prefs()

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

def latest_in_root(root: Path, pattern: str) -> Path | None:
    try:
        files = list(root.glob(pattern))
    except Exception:
        return None
    return max(files, key=lambda p: p.stat().st_mtime) if files else None

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

def format_done(done_line: str, logs_path: str | None = None) -> str:
    msg = "Готово.\n<pre>" + htmllib.escape(done_line) + "</pre>"
    if logs_path:
        msg += "\nЛоги сохранены: <code>" + htmllib.escape(logs_path) + "</code>"
    return msg

def _utc_now_z() -> str:
    return datetime.utcnow().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

def _signal_id_from_published_at_msk(published_at: str) -> str:
    try:
        dt_utc = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
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

def _read_last_signal_json() -> dict | None:
    return _read_last_signal_json_from_root(PROJECT_ROOT)

def _read_last_signal_json_from_root(root: Path) -> dict | None:
    p = root / "logs" / "last.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

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

def _extract_max_wait_minutes(d: dict, default: int = 180) -> int:
    cap = 180
    for k in ("max_wait_minutes", "max_valid_minutes", "validity_minutes", "max_valid_minutes"):
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

def _resolve_signal_mode_for_confirm(d: dict) -> str:
    raw_mode = d.get("mode")
    if isinstance(raw_mode, str):
        mode = raw_mode.strip().lower()
        if mode in VALID_MODES:
            return mode

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
    if isinstance(uid, int):
        return get_user_mode(uid)
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
    mode_key = str(mode or _resolve_signal_mode_for_confirm(d)).strip().lower()
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
) -> dict | None:
    d = _read_last_signal_json()
    if not isinstance(d, dict):
        return None

    symbol = d.get("symbol") or symbol_hint
    direction = d.get("direction") or d.get("side")
    entry_range = d.get("entry_range") if isinstance(d.get("entry_range"), dict) else {}
    entry_low = entry_range.get("min")
    entry_high = entry_range.get("max")
    sl = d.get("sl")
    tp1 = d.get("tp1")
    tp2 = d.get("tp2")

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
    origin_chat_id = _normalize_chat_id(origin_chat_id)
    if origin_chat_id is None:
        origin_chat_id = _normalize_chat_id(channel_id)
    publish_targets = _normalize_chat_id_list(publish_targets)
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
        mode = get_user_mode(uid) if isinstance(uid, int) else "neutral"

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
            low = _try_float(entry_range.get("min"))
            high = _try_float(entry_range.get("max"))
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

        tp_out: dict = {"tp1": None, "tp2": None}
        tp_by_mode = d.get("tp_by_mode")
        tp1_mode = None
        tp2_mode = None
        if isinstance(tp_by_mode, dict):
            tp_m = tp_by_mode.get(mode)
            if isinstance(tp_m, dict):
                tp1_mode = tp_m.get("tvh1")
                tp2_mode = tp_m.get("tvh2")

        tp1_mode_f = _try_float(tp1_mode)
        tp2_mode_f = _try_float(tp2_mode)
        if tp1_mode_f is not None or tp2_mode_f is not None:
            tp_out = {"tp1": tp1_mode_f, "tp2": tp2_mode_f}
        else:
            tp_out = {"tp1": (_try_float(tp1) if tp1 is not None else None), "tp2": (_try_float(tp2) if tp2 is not None else None)}
    except Exception:
        try:
            sl_val = float(sl)
            tp_out = {"tp1": (float(tp1) if tp1 is not None else None), "tp2": (float(tp2) if tp2 is not None else None)}
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

async def _send_to_current_chat_with_kb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup,
) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(text, reply_markup=reply_markup)
        return
    chat = update.effective_chat
    if chat:
        await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=reply_markup)

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

def main_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Сигнал")],
            [KeyboardButton("📈 Анализ")],
            [KeyboardButton("⚙️ Режим")],
        ],
        resize_keyboard=True
    )

def personal_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📣 Последний сигнал")],
            [KeyboardButton("⚙️ Настройки")],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

def personal_settings_kb(uid: int):
    if is_subscribed(uid):
        rows = [[KeyboardButton("❌ Отписаться")]]
    else:
        rows = [[KeyboardButton("✅ Подписаться")]]
    rows.append([KeyboardButton("🎯 Фильтры")])
    rows.append([KeyboardButton("⬅️ Назад")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def filters_menu_kb(uid: int):
    prefs = _get_user_prefs(uid)
    mode = prefs.get("mode") or "all"
    direction = prefs.get("direction") or "all"
    mode_label = {"all": "Все", "aggressive": "Aggressive", "neutral": "Neutral", "conservative": "Conservative"}.get(mode, "Все")
    dir_label = {"all": "Все", "long": "Long", "short": "Short"}.get(direction, "Все")
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(f"🎯 Режим: {mode_label}")],
            [KeyboardButton(f"🧭 Направление: {dir_label}")],
            [KeyboardButton("⬅️ Назад")],
        ],
        resize_keyboard=True,
    )

def personal_help_text() -> str:
    return (
        "Команды:\n"
        "/subscribe — получать сигналы\n"
        "/unsubscribe — остановить\n"
        "/last_signal — показать последний сигнал\n"
        "/whoami — ваш id\n"
        "/help — помощь\n\n"
        "Сигналы приходят автоматически после подписки, "
        "когда публикуются в основном канале.\n\n"
        "Кнопка «⚙️ Настройки» — подписка/отписка.\n"
        "Кнопка «🎯 Фильтры» — режим/направление."
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
    sub = is_subscribed(uid)
    await update.message.reply_text(
        f"whoami\nuid: {uid}\nallowed(env): {ALLOWED_UIDS}\n"
        f"in JSON: {bool(cfg)}\nchannel: {ch}\nmode: {get_user_mode(uid)}\n"
        f"subscribed: {sub}"
    )

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    first = (update.effective_user.first_name or "").strip()
    await update.message.reply_text(f"Привет, {first or 'трейдер'}!")
    if is_subscribed(uid):
        await update.message.reply_text("✅ Подписка активна. Для отключения: /unsubscribe")
    else:
        await update.message.reply_text("Чтобы получать сигналы: /subscribe")
    await _send_to_current_chat_with_kb(update, context, personal_help_text(), personal_menu_kb())

async def subscribe(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if subscribe_user(uid):
        _log_subscription_event("subscribed", update.effective_user)
        if not _get_user_prefs(uid):
            _set_user_prefs(uid, {"mode": "all", "direction": "all"})
        await _send_to_current_chat(update, context, "✅ Подписка включена. Чтобы отключить: /unsubscribe")
        await last_signal(update, context)
    else:
        await _send_to_current_chat(update, context, "ℹ️ Вы уже подписаны. Для отключения: /unsubscribe")
    await _send_to_current_chat_with_kb(update, context, "⚙️ Настройки обновлены.", personal_settings_kb(uid))

async def unsubscribe(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if unsubscribe_user(uid):
        _log_subscription_event("unsubscribed", update.effective_user)
        await _send_to_current_chat(update, context, "❌ Подписка отключена. Вернуться: /subscribe")
    else:
        await _send_to_current_chat(update, context, "ℹ️ Вы не были подписаны. Подписаться: /subscribe")
    await _send_to_current_chat_with_kb(update, context, "⚙️ Настройки обновлены.", personal_settings_kb(uid))

async def last_signal(update:Update, context:ContextTypes.DEFAULT_TYPE):
    sig_html = latest_in_root(SIGNAL_ROOT, "signal_*.html")
    if not sig_html:
        await _send_to_current_chat(update, context, "Нет сохранённых сигналов.")
        return
    parts = html_file_to_tg_text(Path(sig_html))
    if not parts:
        await _send_to_current_chat(update, context, "Нет сохранённых сигналов.")
        return
    for idx, part in enumerate(parts):
        prefix = "📣 Последний сигнал\n\n" if idx == 0 else ""
        await _send_to_current_chat(update, context, prefix + part)

async def help_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await _send_to_current_chat_with_kb(update, context, personal_help_text(), personal_menu_kb())

async def handle_settings_menu(update, context):
    uid = update.effective_user.id
    await _send_to_current_chat_with_kb(
        update,
        context,
        "⚙️ Настройки подписки",
        personal_settings_kb(uid),
    )

async def handle_filters_menu(update, context):
    uid = update.effective_user.id
    await _send_to_current_chat_with_kb(
        update,
        context,
        "🎯 Фильтры сигналов",
        filters_menu_kb(uid),
    )

async def handle_filter_mode_toggle(update, context):
    uid = update.effective_user.id
    prefs = _get_user_prefs(uid)
    order = ["all", "aggressive", "neutral", "conservative"]
    current = prefs.get("mode") or "all"
    try:
        idx = order.index(current)
    except ValueError:
        idx = 0
    prefs["mode"] = order[(idx + 1) % len(order)]
    _set_user_prefs(uid, prefs)
    await _send_to_current_chat_with_kb(
        update,
        context,
        "🎯 Фильтры обновлены.",
        filters_menu_kb(uid),
    )

async def handle_filter_direction_toggle(update, context):
    uid = update.effective_user.id
    prefs = _get_user_prefs(uid)
    order = ["all", "long", "short"]
    current = prefs.get("direction") or "all"
    try:
        idx = order.index(current)
    except ValueError:
        idx = 0
    prefs["direction"] = order[(idx + 1) % len(order)]
    _set_user_prefs(uid, prefs)
    await _send_to_current_chat_with_kb(
        update,
        context,
        "🎯 Фильтры обновлены.",
        filters_menu_kb(uid),
    )

async def handle_back(update,context):
    await _send_to_current_chat_with_kb(update, context, "📋 Меню", personal_menu_kb())

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

# ---------- FULL ----------
async def handle_full(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
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
    target = get_main_publication_chat_id(uid)
    main_targets = get_main_publication_targets(uid)
    targets = get_signal_targets(uid)

    if not acquire_gen_lock():
        await msg.edit_text("Занято, попробуй позже.")
        return

    try:
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
                for ch in targets:
                    await context.bot.send_message(chat_id=ch, text=part0)
                if "📌 Сигнал не выдан" in part0:
                    if target is not None:
                        payload = _build_no_trade_decision_payload(
                            uid,
                            tg_text=part0,
                            symbol_hint=None,
                            channel_id=target,
                            origin_chat_id=target,
                            publish_targets=main_targets,
                        )
                        if payload and _should_send_to_aia_for_target(target):
                            asyncio.create_task(_send_no_trade_decision_to_aia_background(payload))
                else:
                    published_at = _utc_now_z()
                    signal_id = _infer_signal_id(
                        Path(sig_html) if sig_html else None,
                        Path(run_log) if run_log else None,
                        published_at,
                    )
                    global _AIA_UID_CONTEXT
                    _AIA_UID_CONTEXT = uid
                    if target is not None:
                        sig_v1 = _build_signal_json_v1(
                            signal_id=signal_id,
                            published_at=published_at,
                            channel_id=target,
                            origin_chat_id=target,
                            publish_targets=main_targets,
                            symbol_hint=None,
                        )
                        if sig_v1 and _should_send_to_aia_for_target(target):
                            asyncio.create_task(_send_signal_to_aia_background(sig_v1))
        else:
            await msg.edit_text("Не удалось сформировать publish HTML для текущего прогона.")
            return

        mode_label = MODE_LABELS.get(get_user_mode(uid), MODE_LABELS["neutral"])
        await msg.edit_text(
            format_done(
                f"✅ FULL: режим {mode_label}",
                logs_path=_relpath(run_log) if run_log else None,
            ),
            parse_mode=ParseMode.HTML
        )

    finally:
        release_gen_lock()

# ---------- ANALYSIS ----------
async def handle_current_analysis(update,context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
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
    target = get_main_chat_id(uid)

    if not acquire_gen_lock():
        await msg.edit_text("Занято, позже.")
        return

    try:
        set_params_mode(get_user_mode(uid))
        proc = subprocess.run(
            ["bash","-lc", f"cd '{PROJECT_ROOT}' && SIGNAL_SKIP_AIA_SEND=1 ./signal full"],
            capture_output=True, text=True, timeout=900
        )
        analysis = latest("analysis_*.md")
        _, run_log = _resolve_signal_run_artifacts(proc)
        if not analysis:
            await msg.edit_text("Не удалось сформировать анализ.")
            return

        hdr = make_header("📝 LLM Анализ")
        txt_raw = Path(analysis).read_text(encoding="utf-8")
        txt = strip_snapshot(txt_raw).split("2️⃣ Сетап")[0].strip()
        await context.bot.send_message(chat_id=target, text=hdr+"\n\n"+txt)

        await msg.edit_text(
            format_done(
                f"✅ CURRENT: {_relpath(Path(analysis))}",
                logs_path=_relpath(run_log) if run_log else None,
            ),
            parse_mode=ParseMode.HTML
        )
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
    target = get_main_publication_chat_id(uid)
    main_targets = get_main_publication_targets(uid)
    targets = get_signal_targets(uid)

    if is_gen_locked():
        await msg.edit_text("Уже считается другой сигнал, попробуй позже.")
        return

    touch_request(uid)

    if not acquire_gen_lock():
        await msg.edit_text("Уже в работе, позже.")
        return

    try:
        set_params_mode(get_user_mode(uid))
        # SINGLE-путь: один символ напрямую
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
                for ch in targets:
                    await context.bot.send_message(chat_id=ch, text=part0)
                if "📌 Сигнал не выдан" in part0:
                    if target is not None:
                        payload = _build_no_trade_decision_payload(
                            uid,
                            tg_text=part0,
                            symbol_hint=symbol,
                            channel_id=target,
                            origin_chat_id=target,
                            publish_targets=main_targets,
                        )
                        if payload and _should_send_to_aia_for_target(target):
                            asyncio.create_task(_send_no_trade_decision_to_aia_background(payload))
                else:
                    published_at = _utc_now_z()
                    signal_id = _infer_signal_id(
                        Path(sig_html) if sig_html else None,
                        Path(run_log) if run_log else None,
                        published_at,
                    )
                    global _AIA_UID_CONTEXT
                    _AIA_UID_CONTEXT = uid
                    if target is not None:
                        sig_v1 = _build_signal_json_v1(
                            signal_id=signal_id,
                            published_at=published_at,
                            channel_id=target,
                            origin_chat_id=target,
                            publish_targets=main_targets,
                            symbol_hint=symbol,
                        )
                        if sig_v1 and _should_send_to_aia_for_target(target):
                            asyncio.create_task(_send_signal_to_aia_background(sig_v1))
        else:
            await msg.edit_text("Не удалось сформировать publish HTML для текущего прогона.")
            return

        await msg.edit_text(
            format_done(
                f"✅ SIGNAL: {symbol}",
                logs_path=_relpath(run_log) if run_log else None,
            ),
            parse_mode=ParseMode.HTML
        )

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
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^✅ Подписаться$"), subscribe))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^❌ Отписаться$"), unsubscribe))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📣 Последний сигнал$"), last_signal))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⚙️ Настройки$"), handle_settings_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🎯 Фильтры$"), handle_filters_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🎯 Режим:"), handle_filter_mode_toggle))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🧭 Направление:"), handle_filter_direction_toggle))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^ℹ️ Помощь$"), help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📊 Сигнал$"), handle_signal_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📈 Анализ$"), handle_analysis_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📈 Current$"), handle_current_analysis))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⚙️ Режим$"), handle_mode_menu))
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
        raise SystemExit("Set TELEGRAM_PERSONAL_BOT_TOKEN in .env.tg.clean")
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("last_signal", last_signal))
    app.add_handler(CommandHandler("aia_report", handle_aia_report))
    app.add_handler(CommandHandler("aia_daily", handle_aia_daily))
    register_text_handlers(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
