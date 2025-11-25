#!/usr/bin/env python3
from zoneinfo import ZoneInfo
import os
import subprocess
import re
import html as htmllib
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===== базовая инициализация =====

BASE = Path(__file__).resolve().parent
# Корень проекта с сигналами: по умолчанию = папка, где лежит tg_bot.py.
# Можно переопределить через переменную окружения LLM_SIGNAL_ROOT.
PROJECT_ROOT = Path(os.getenv("LLM_SIGNAL_ROOT", str(BASE)))

load_dotenv(BASE / ".env.tg.clean")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL = os.getenv("TELEGRAM_TARGET_CHANNEL")


def parse_allowed_ids() -> list[int]:
    ids: list[int] = []
    raw_multi = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    raw_legacy = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    for raw in (raw_multi, raw_legacy):
        for x in raw.replace(";", ",").split(","):
            x = x.strip()
            if x and x.lstrip("-").isdigit():
                ids.append(int(x))
    return list(dict.fromkeys(ids))  # уникальные


ALLOWED_UIDS = parse_allowed_ids()

# ===== список тикеров для single =====

try:
    _pool = json.load(
        open(PROJECT_ROOT / "pool.json", "r", encoding="utf-8")
    )["pool"]
    SYMBOLS = [s.split("/")[0] for s in _pool]
except Exception:
    SYMBOLS = ["BTC", "ETH", "SOL", "AVAX", "APT", "AAVE", "LINK", "TON", "ARB"]

SYMBOLS_SET = set(SYMBOLS)

GREETINGS = {87017886: "Привет, Ирина! 👋"}


def make_header(title: str) -> str:
    """Единый формат заголовков; время — МСК."""
    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    return f"{title} • {now_msk.strftime('%d.%m.%Y %H:%M')}"


def is_allowed(uid: int) -> bool:
    return (uid in ALLOWED_UIDS) if ALLOWED_UIDS else False


def latest(pattern: str) -> Path | None:
    files = list(PROJECT_ROOT.glob(pattern))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def html_file_to_tg_text(p: Path, max_len: int = 4000) -> list[str]:
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s).strip()
    chunks: list[str] = []
    while s:
        chunks.append(s[:max_len])
        s = s[max_len:]
    return chunks


def md_file_to_chunks(p: Path, max_len: int = 4000) -> list[str]:
    s = p.read_text(encoding="utf-8").strip()
    chunks: list[str] = []
    while s:
        chunks.append(s[:max_len])
        s = s[max_len:]
    return chunks


# === утилита: удалить блок SNAPSHOT в тексте анализа ===
def strip_snapshot(text: str) -> str:
    # удаляем блок от '=== [SNAPSHOT ДЛЯ LLM] ===' до '=========================='
    return re.sub(
        r"(?s)^=== \[SNAPSHOT ДЛЯ LLM\] ===.*?==========================\n?",
        "",
        text,
    ).strip()


# ===== меню =====

def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("📊 Сигнал")],
        [KeyboardButton("📈 Анализ")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def signal_menu_kb() -> ReplyKeyboardMarkup:
    try:
        pool = json.load(
            open(PROJECT_ROOT / "pool.json", "r", encoding="utf-8")
        )["pool"]
    except Exception:
        pool = [
            "BTC/USDT",
            "ETH/USDT",
            "SOL/USDT",
            "AVAX/USDT",
            "APT/USDT",
            "AAVE/USDT",
            "LINK/USDT",
            "TON/USDT",
            "ARB/USDT",
        ]
    short = [s.split("/")[0] for s in pool]
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton("🤖 Auto (FULL)")],
        [KeyboardButton("🗓 DAY"), KeyboardButton("📰 MID")],
    ]
    for i in range(0, len(short), 3):
        rows.append([KeyboardButton(x) for x in short[i : i + 3]])
    rows.append([KeyboardButton("⬅️ Назад")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ===== handlers =====

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        f"whoami\n- your id: {uid}\n- allowed: {ALLOWED_UIDS}\n- channel: {CHANNEL}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await update.message.reply_text("Доступ запрещён.")
        return
    first = (update.effective_user.first_name or "").strip()
    hello = GREETINGS.get(uid) or (f"Привет, {first}!" if first else "Привет!")
    await update.message.reply_text(hello)
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())


async def handle_signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери актив или режим:", reply_markup=signal_menu_kb()
    )


async def handle_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    header = make_header("📝 LLM Full анализ")
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return

    msg = await update.message.reply_text(
        "Запускаю FULL анализ… это займёт немного времени."
    )
    proc = subprocess.run(
        ["bash", "-lc", f"cd '{PROJECT_ROOT}' && ./signal full"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    analysis = latest("analysis_*.md")
    sig_html = latest("signal_*.html")

    if analysis:
        text_raw = Path(analysis).read_text(encoding="utf-8")
        text = strip_snapshot(text_raw).split("2️⃣ Сетап")[0].strip()
        await context.bot.send_message(chat_id=CHANNEL, text=header + "\n\n" + text)

    if sig_html:
        parts = html_file_to_tg_text(Path(sig_html))
        await context.bot.send_message(chat_id=CHANNEL, text="📣 Сигнал\n\n" + parts[0])

    safe_tail = htmllib.escape(
        "\n".join(proc.stdout.splitlines()[-20:]) or "(лог пуст)"
    )
    await msg.edit_text(
        f"Готово\n<pre>{safe_tail}</pre>", parse_mode=ParseMode.HTML
    )


async def handle_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    header = make_header("📝 LLM Анализ")
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await update.message.reply_text("Доступ запрещён.")
        return

    msg = await update.message.reply_text(
        "Запускаю анализ рынка… это займёт немного времени."
    )
    proc = subprocess.run(
        ["bash", "-lc", f"cd '{PROJECT_ROOT}' && ./signal full"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    analysis = latest("analysis_*.md")
    if not analysis:
        await msg.edit_text("Не удалось сформировать анализ.")
        return

    text_raw = Path(analysis).read_text(encoding="utf-8")
    text = strip_snapshot(text_raw).split("2️⃣ Сетап")[0].strip()
    await context.bot.send_message(chat_id=CHANNEL, text=header + "\n" + text)

    safe_tail = htmllib.escape(
        "\n".join(proc.stdout.splitlines()[-20:]) or "(лог пуст)"
    )
    await msg.edit_text(
        f"Готово\n<pre>{safe_tail}</pre>", parse_mode=ParseMode.HTML
    )


# ===== SINGLE-AS-FULL =====

async def handle_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return

    sym_text = (update.message.text or "").strip().upper()
    if sym_text not in SYMBOLS_SET:
        await update.message.reply_text("Не распознал символ. Выбери из меню.")
        return

    symbol = f"{sym_text}/USDT"
    msg = await update.message.reply_text(f"Готовлю сигнал по {symbol}…")

    # Временная подмена pool.json одним инструментом
    _pool_path = PROJECT_ROOT / "pool.json"
    _bak_path = PROJECT_ROOT / "pool._bak_"
    try:
        if _pool_path.exists():
            _bak_path.write_text(_pool_path.read_text(encoding="utf-8"), encoding="utf-8")
        _pool_path.write_text(json.dumps({"pool": [symbol]}), encoding="utf-8")
    except Exception:
        pass

    proc = subprocess.run(
        ["bash", "-lc", f"cd '{PROJECT_ROOT}' && ./signal full"],
        capture_output=True,
        text=True,
        timeout=900,
    )

    try:
        if _bak_path.exists():
            _pool_path.write_text(_bak_path.read_text(encoding="utf-8"), encoding="utf-8")
            _bak_path.unlink()
    except Exception:
        pass

    # Анализ (как в FULL), но без SNAPSHOT
    analysis = latest("analysis_*.md")
    if analysis:
        _hdr = make_header("📝 LLM Full анализ")
        _txt_raw = Path(analysis).read_text(encoding="utf-8")
        _txt = strip_snapshot(_txt_raw).split("2️⃣ Сетап")[0].strip()
        await context.bot.send_message(chat_id=CHANNEL, text=_hdr + "\n\n" + _txt)

    # Сигнал HTML
    sig_html = latest("signal_*.html")
    if sig_html:
        parts = html_file_to_tg_text(Path(sig_html))
        if parts:
            await context.bot.send_message(chat_id=CHANNEL, text="📣 Сигнал\n\n" + parts[0])

    # Мини-блок
    try:
        last_raw = latest("logs/last.raw.json")
        if last_raw:
            data = json.loads(Path(last_raw).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sym = data.get("symbol") or symbol
                er = data.get("entry_range") or {}
                er_min = er.get("min") if er.get("min") is not None else "—"
                er_max = er.get("max") if er.get("max") is not None else "—"
                entry_mode = data.get("entry_mode") or "limit"
                confidence = data.get("confidence") or "Medium"
                confirm = (
                    data.get("confirmation_rules")
                    or data.get("break_even_rule")
                    or ""
                )
                confirm = (confirm or "—").strip()
                mini = (
                    "📊 " + str(sym) + "\n"
                    "🎯 " + str(er_min) + "–" + str(er_max)
                    + "  |  " + entry_mode + "  |  " + confidence + "\n"
                    "☑ " + confirm
                )
                await context.bot.send_message(chat_id=CHANNEL, text=mini)
    except Exception:
        pass

    # хвост лога пользователю
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-20:])
    await msg.edit_text(
        "Готово\n<pre>" + htmllib.escape(tail or "(лог пуст)") + "</pre>",
        parse_mode=ParseMode.HTML,
    )


# ===== публикация отчётов DAY/MID =====

async def _post_report(
    dir_glob: str,
    header_emoji: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    roots_dir = "day" if "day" in dir_glob else "mid"
    root = PROJECT_ROOT / "reports" / roots_dir
    roots = sorted(root.glob("*"))
    if not roots:
        await update.message.reply_text("Отчёт не найден.")
        return

    d = roots[-1]
    header = make_header(f"{header_emoji} {'DAY' if roots_dir == 'day' else 'MID'}")
    header_line = (
        f"<b><u>{header_emoji} {'DAY' if roots_dir=='day' else 'MID'} REPORT</u></b>\n"
    )
    await context.bot.send_message(
        chat_id=CHANNEL, text=header_line, parse_mode=ParseMode.HTML
    )

    an = sorted(d.glob("analysis_*.md"))
    if an:
        text = an[-1].read_text(encoding="utf-8").strip()
        if text:
            await context.bot.send_message(
                chat_id=CHANNEL, text=header + "\n\n" + text[:3900]
            )

    # Для DAY — сигнал не отправляем (по ТЗ)
    if roots_dir == "day":
        return


async def handle_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if ALLOWED_UIDS and uid not in ALLOWED_UIDS:
        await update.message.reply_text("Нет доступа.")
        return

    msg = await update.message.reply_text("Готовлю DAY (1 день)…")
    proc = subprocess.run(
        [
            "bash",
            "-lc",
            f"cd '{PROJECT_ROOT}' && chmod +x run_day.sh && ./run_day.sh",
        ],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    await _post_report("reports/day", "🗓", update, context)
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-20:])
    await msg.edit_text(
        "Готово\n<pre>" + htmllib.escape(tail or "(лог пуст)") + "</pre>",
        parse_mode=ParseMode.HTML,
    )


async def handle_mid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if ALLOWED_UIDS and uid not in ALLOWED_UIDS:
        await update.message.reply_text("Нет доступа.")
        return

    msg = await update.message.reply_text("Готовлю MID (1–2 недели)…")
    proc = subprocess.run(
        [
            "bash",
            "-lc",
            f"cd '{PROJECT_ROOT}' && chmod +x run_mid.sh && ./run_mid.sh",
        ],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    await _post_report("reports/mid", "📰", update, context)
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-20:])
    await msg.edit_text(
        "Готово\n<pre>" + htmllib.escape(tail or "(лог пуст)") + "</pre>",
        parse_mode=ParseMode.HTML,
    )


# ===== регистрация =====

def register_text_handlers(app: Application):
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📊 Сигнал$"),
            handle_signal_menu,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📈 Анализ$"),
            handle_analysis,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^🗓 DAY$"),
            handle_day,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📰 MID$"),
            handle_mid,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^🤖 Auto \\(FULL\\)$"),
            handle_full,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^⬅️ Назад$"),
            handle_back,
        )
    )
    sym_regex = "^(" + "|".join(sorted(SYMBOLS_SET)) + ")$"
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(sym_regex),
            handle_symbol,
        )
    )


# ===== main =====

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_TARGET_CHANNEL in .env.tg.clean"
        )
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    register_text_handlers(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
