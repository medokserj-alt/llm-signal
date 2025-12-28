#!/usr/bin/env python3
from zoneinfo import ZoneInfo
import asyncio
import os, json, time, subprocess, re, html as htmllib, tempfile
from datetime import datetime
from pathlib import Path
from urllib import request as urlrequest, parse as urlparse

from dotenv import load_dotenv
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from pinned_state import get_pinned_message_id, load_pinned_state, save_pinned_state, set_pinned_message_id
from rbac import analysis_menu_layout, is_admin

# ============== BASE / ENV ==================

BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE

load_dotenv(BASE / ".env.tg.clean")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FALLBACK_CHANNEL = os.getenv("TELEGRAM_TARGET_CHANNEL")

# ============== AIA (AI Agent) ==============

AIA_BASE_URL = "http://127.0.0.1:8002"
AIA_TIMEOUT_SEC = 2.5

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
PARAMS_PATH = PROJECT_ROOT / "params.json"
PINNED_STATE_PATH = PROJECT_ROOT / "pinned_state.json"
USER_CONFIG = {}
GLOBAL_SETTINGS = {"lock_timeout_sec": 120}
VALID_MODES = {"aggressive", "neutral", "conservative"}

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

def get_user_cfg(uid:int):
    return USER_CONFIG.get(str(uid))

def get_main_chat_id(uid:int):
    cfg = get_user_cfg(uid)
    if cfg:
        ch = cfg.get("channels",{}).get("main_chat_id")
        if ch:
            return ch
    return FALLBACK_CHANNEL

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
    if ALLOWED_UIDS and uid in ALLOWED_UIDS:
        return True
    return get_user_cfg(uid) is not None

# ============== HELPERS =====================

def make_header(title:str)->str:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    return f"{title} • {now.strftime('%d.%m.%Y %H:%M')}"

def latest(pattern:str):
    files = list(PROJECT_ROOT.glob(pattern))
    return max(files, key=lambda p:p.stat().st_mtime) if files else None

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

def _infer_signal_id(sig_html: Path | None, run_log: Path | None, published_at: str) -> str:
    for p in (sig_html, run_log):
        if not p:
            continue
        m = re.search(r"_(\d{8}_\d{6})\.", p.name)
        if m:
            return m.group(1)
    # fallback: YYYYMMDD_HHMMSS from published_at
    try:
        dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y%m%d_%H%M%S")
    except Exception:
        return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

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
    p = PROJECT_ROOT / "logs" / "last.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _build_signal_json_v1(*, signal_id: str, published_at: str, channel_id, symbol_hint: str | None = None) -> dict | None:
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
    if entry_low is None or entry_high is None or sl is None:
        return None

    try:
        entry_zone = [float(entry_low), float(entry_high)]
        sl_val = float(sl)
        tp_out: dict = {"tp1": (float(tp1) if tp1 is not None else None), "tp2": (float(tp2) if tp2 is not None else None)}
    except Exception:
        return None

    try:
        if isinstance(channel_id, str) and channel_id.strip().lstrip("-").isdigit():
            channel_id = int(channel_id.strip())
    except Exception:
        pass

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

        market_phase = None
        if entry_type == "breakout":
            market_phase = "impulse"
        elif entry_type == "wait_confirm":
            market_phase = "range"
        elif entry_type == "pullback":
            market_phase = "consolidation"

        if (
            mode in ("aggressive", "neutral", "conservative")
            and entry_type in ("pullback", "breakout", "wait_confirm")
            and market_phase in ("impulse", "consolidation", "range")
        ):
            meta = {"mode": mode, "entry_type": entry_type, "market_phase": market_phase}
    except Exception:
        meta = None

    out = {
        "signal_id": str(signal_id),
        "symbol": str(symbol),
        "direction": str(direction),
        "entry_zone": entry_zone,
        "sl": sl_val,
        "tp": tp_out,
        "published_at": str(published_at),
        "channel_id": channel_id,
    }
    if meta is not None:
        out["meta"] = meta
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

def _build_no_trade_decision_payload(uid: int, *, tg_text: str, symbol_hint: str | None) -> dict | None:
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

    return {
        "signal_candidate": {"asset": str(asset), "direction": direction, "mode": mode},
        "reason_code": reason_code,
        "reason_text": reason_text,
        "market_phase": market_phase,
        "entry_type": entry_type,
        "evaluated_at": _utc_now_z(),
    }

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
        pool = json.load(open(PROJECT_ROOT/"pool.json","r",encoding="utf-8"))["pool"]
        return [s.split("/")[0] for s in pool]
    except Exception:
        return ["BTC","ETH","SOL","AVAX","SUI","APT","AAVE","LINK","TON","ARB"]

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
    "aggressive": "🟥 Агрессивный — максимум сигналов, мягкие фильтры риска.",
    "neutral": "🟨 Нейтральный — баланс сигнальных фильтров и частоты входов.",
    "conservative": "🟩 Консервативный — меньше сигналов, строгие фильтры.",
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
    await update.message.reply_text(
        f"whoami\nuid: {uid}\nallowed(env): {ALLOWED_UIDS}\nin JSON: {bool(cfg)}\nchannel: {ch}\nmode: {get_user_mode(uid)}"
    )

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Доступ запрещён.")
        return
    first = (update.effective_user.first_name or "").strip()
    await update.message.reply_text(f"Привет, {first or 'трейдер'}!")
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())

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
    target = get_main_chat_id(uid)

    if not acquire_gen_lock():
        await msg.edit_text("Занято, попробуй позже.")
        return

    try:
        set_params_mode(get_user_mode(uid))
        proc = subprocess.run(
            ["bash","-lc", f"cd '{PROJECT_ROOT}' && ./signal full"],
            capture_output=True, text=True, timeout=900
        )

        analysis = latest("analysis_*.md")
        sig_html = latest("signal_*.html")
        run_log = latest("logs/signal_*.log")

        if analysis:
            hdr = make_header("📝 LLM Full анализ")
            txt_raw = Path(analysis).read_text(encoding="utf-8")
            txt = strip_snapshot(txt_raw).split("2️⃣ Сетап")[0].strip()
            await context.bot.send_message(chat_id=target, text=hdr+"\n\n"+txt)

        if sig_html:
            parts = html_file_to_tg_text(Path(sig_html))
            if parts:
                part0 = parts[0]
                await context.bot.send_message(chat_id=target, text="📣 Сигнал\n\n"+part0)
                if "📌 Сигнал не выдан" in part0:
                    payload = _build_no_trade_decision_payload(uid, tg_text=part0, symbol_hint=None)
                    if payload:
                        asyncio.create_task(_send_no_trade_decision_to_aia_background(payload))
                published_at = _utc_now_z()
                signal_id = _infer_signal_id(Path(sig_html) if sig_html else None, Path(run_log) if run_log else None, published_at)
                sig_v1 = _build_signal_json_v1(
                    signal_id=signal_id,
                    published_at=published_at,
                    channel_id=target,
                    symbol_hint=None,
                )
                if sig_v1:
                    asyncio.create_task(_send_signal_to_aia_background(sig_v1))

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
            ["bash","-lc", f"cd '{PROJECT_ROOT}' && ./signal full"],
            capture_output=True, text=True, timeout=900
        )
        analysis = latest("analysis_*.md")
        run_log = latest("logs/signal_*.log")
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
    target = get_main_chat_id(uid)

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
        sig_html = latest("signal_*.html")
        run_log = latest("logs/signal_*.log")

        if analysis:
            hdr = make_header(f"📝 Анализ {symbol}")
            txt_raw = Path(analysis).read_text(encoding="utf-8")
            txt = strip_snapshot(txt_raw).strip()   # ВЕСЬ анализ, не режем по 2️⃣ Сетап
            await context.bot.send_message(chat_id=target, text=hdr+"\n\n"+txt)

        if sig_html:
            parts = html_file_to_tg_text(Path(sig_html))
            if parts:
                part0 = parts[0]
                await context.bot.send_message(chat_id=target, text="📣 Сигнал\n\n"+part0)
                if "📌 Сигнал не выдан" in part0:
                    payload = _build_no_trade_decision_payload(uid, tg_text=part0, symbol_hint=symbol)
                    if payload:
                        asyncio.create_task(_send_no_trade_decision_to_aia_background(payload))
                published_at = _utc_now_z()
                signal_id = _infer_signal_id(Path(sig_html) if sig_html else None, Path(run_log) if run_log else None, published_at)
                sig_v1 = _build_signal_json_v1(
                    signal_id=signal_id,
                    published_at=published_at,
                    channel_id=target,
                    symbol_hint=symbol,
                )
                if sig_v1:
                    asyncio.create_task(_send_signal_to_aia_background(sig_v1))

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
    hdr = make_header(f"{emoji} {root_dir.upper()}")
    header_line = f"<b><u>{emoji} {root_dir.upper()} REPORT</u></b>\n"
    header_msg = await context.bot.send_message(chat_id=channel, text=header_line, parse_mode=ParseMode.HTML)
    an = sorted(d.glob("analysis_*.md"))
    report_msg = None
    if an:
        txt = an[-1].read_text(encoding="utf-8").strip()
        report_msg = await context.bot.send_message(chat_id=channel, text=hdr+"\n\n"+txt[:3900])

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
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aia_report", handle_aia_report))
    app.add_handler(CommandHandler("aia_daily", handle_aia_daily))
    register_text_handlers(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
