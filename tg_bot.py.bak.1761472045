#!/usr/bin/env python3
import os, time, subprocess, pathlib, re, html as htmllib, json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

# ===== базовая инициализация =====
BASE = pathlib.Path(__file__).resolve().parent
load_dotenv(BASE/".env.tg")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL   = os.getenv("TELEGRAM_TARGET_CHANNEL")

def parse_allowed_ids() -> list[int]:
    ids = []
    raw_multi  = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    raw_legacy = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    for raw in (raw_multi, raw_legacy):
        for x in raw.replace(";", ",").split(","):
            x = x.strip()
            if x and (x.lstrip("-").isdigit()):
                try:
                    ids.append(int(x))
                except:
                    pass
    return list(dict.fromkeys(ids))

ALLOWED_UIDS = parse_allowed_ids()

def latest(pattern: str):
    files = list(BASE.glob(pattern))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None

def html_file_to_tg_text(p: pathlib.Path, max_len=4000):
    s = p.read_text(encoding="utf-8")
    m = re.search(r'<body[^>]*>(.*?)</body>', s, flags=re.S|re.I)
    s = m.group(1) if m else s
    s = re.sub(r'<\s*(strong)\s*>', '<b>', s, flags=re.I)
    s = re.sub(r'<\s*/\s*(strong)\s*>', '</b>', s, flags=re.I)
    s = re.sub(r'<\s*(em)\s*>', '<i>', s, flags=re.I)
    s = re.sub(r'<\s*/\s*(em)\s*>', '</i>', s, flags=re.I)
    s = re.sub(r'<\s*br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'</\s*p\s*>', '\n\n', s, flags=re.I)
    s = re.sub(r'<\s*p[^>]*>', '', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = htmllib.unescape(s)
    s = re.sub(r'[ \t]+\n', '\n', s).strip()
    chunks = []
    while s:
        chunks.append(s[:max_len])
        s = s[max_len:]
    return chunks

def md_file_to_chunks(p: pathlib.Path, max_len=4000):
    s = p.read_text(encoding="utf-8").strip()
    chunks = []
    while s:
        chunks.append(s[:max_len])
        s = s[max_len:]
    return chunks

def is_allowed(uid: int) -> bool:
    return (uid in ALLOWED_UIDS) if ALLOWED_UIDS else False

# ===== feedback (опционально) =====
try:
    from feedback_writer import save_feedback  # файл может отсутствовать — ок
    FEEDBACK_AVAILABLE = True
except Exception:
    save_feedback = None
    FEEDBACK_AVAILABLE = False

# ===== JSON-парсер из текста /feedback =====
def extract_json_from_text(text: str) -> str | None:
    if not text:
        return None
    text = re.sub(r'^\s*/feedback\s*', '', text, flags=re.I).strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.S|re.I)
    if fence:
        return fence.group(1).strip()
    inline = re.search(r"`\s*(\{.+\})\s*`", text, flags=re.S)
    if inline:
        return inline.group(1).strip()
    if text.lstrip().startswith("{") and text.rstrip().endswith("}"):
        return text.strip()
    return None

# ===== main menu keyboard (одна кнопка в строке, «крупнее») =====
def main_menu_kb():
    from telegram import ReplyKeyboardMarkup, KeyboardButton
    kb = [
        [KeyboardButton("📊 Сигнал")],
        [KeyboardButton("📈 Анализ")],
            ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)
# ===== Signal submenu (пары без /USDT в тексте, но с /USDT в callback) =====
def signal_menu_kb():
    try:
        pool = json.load(open(BASE/"pool.json","r",encoding="utf-8"))["pool"]
    except Exception:
        pool = ["BTC/USDT","ETH/USDT","SOL/USDT","AVAX/USDT","APT/USDT","AAVE/USDT","LINK/USDT","TON/USDT","ARB/USDT"]
    rows = []
    for sym in pool:
        label = sym.split("/")[0]  # текст без /USDT
        rows.append([InlineKeyboardButton(f"• {label}", callback_data=f"signal_pair:{sym}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(rows)

# ===== handlers =====
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        "whoami\n"
        f"- your id: {uid}\n"
        f"- allowed: {ALLOWED_UIDS}\n"
        f"- channel: {CHANNEL}"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await update.message.reply_text("Доступ запрещён.")
        return
    # Пустой текст (почти невидимый символ), только кнопки
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())

async def back_to_main_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.edit_text("‎", reply_markup=main_menu_kb())

async def open_signal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await q.answer("Нет доступа", show_alert=True); return
    await q.answer()
    await q.message.edit_text("‎", reply_markup=signal_menu_kb())

async def run_full_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await query.answer("Нет доступа", show_alert=True); return
    await query.answer()
    msg = await query.message.reply_text("Запускаю FULL анализ… это займёт немного времени.")

    t0 = time.time()
    proc = subprocess.run(
        ["bash","-lc","cd ~/llm-signal && ./signal full"],
        capture_output=True, text=True, timeout=900
    )
    t1 = time.time()

    tail = "\n".join(proc.stdout.strip().splitlines()[-20:])
    safe_tail = htmllib.escape(tail)
    await msg.edit_text(f"Готово за {t1-t0:.1f}s\n<b>Хвост лога:</b>\n<pre>{safe_tail}</pre>",
                        parse_mode=ParseMode.HTML)

    analysis = latest("analysis_*.md")
    sig_html = latest("signal_*.html")

    if analysis:
        header = f"📝 LLM Full анализ • {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        parts = md_file_to_chunks(analysis)
        await context.bot.send_message(chat_id=CHANNEL, text=header + parts[0])
        for chunk in parts[1:]:
            await context.bot.send_message(chat_id=CHANNEL, text=chunk)

    if sig_html:
        parts = html_file_to_tg_text(sig_html)
        parts[0] = "📣 Сигнал\n\n" + parts[0]
        await context.bot.send_message(chat_id=CHANNEL, text=parts[0], parse_mode=ParseMode.HTML)
        for chunk in parts[1:]:
            await context.bot.send_message(chat_id=CHANNEL, text=chunk, parse_mode=ParseMode.HTML)

async def signal_pair_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id if update.effective_user else 0
    if not is_allowed(uid):
        await q.answer("Нет доступа", show_alert=True); return
    await q.answer()

    m = re.match(r"^signal_pair:(.+)$", q.data or "")
    if not m:
        await q.message.reply_text("Ошибка: не распознан символ.")
        return
    symbol = m.group(1)

    msg = await q.message.reply_text(f"Готовлю сигнал по {symbol}…")

    # single-режим
    proc = subprocess.run(
        ["bash","-lc", f"cd ~/llm-signal && ./signal --symbol '{symbol}'"],
        capture_output=True, text=True, timeout=600
    )

    # отправка в канал (как раньше)
    sig_html = latest("signal_*.html")
    if sig_html:
        parts = html_file_to_tg_text(sig_html)
        parts[0] = "📣 Сигнал\n\n" + parts[0]
        await context.bot.send_message(chat_id=CHANNEL, text=parts[0], parse_mode=ParseMode.HTML)
        for chunk in parts[1:]:
            await context.bot.send_message(chat_id=CHANNEL, text=chunk, parse_mode=ParseMode.HTML)

    tail = "\n".join(proc.stdout.strip().splitlines()[-20:])
    safe_tail = htmllib.escape(tail or "(лог пуст)")
    await msg.edit_text(f"Готово\n<pre>{safe_tail}</pre>", parse_mode=ParseMode.HTML)

# ===== main =====
def register_text_handlers(app):
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📊 Сигнал$"), handle_signal_menu))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📈 Анализ$"), handle_analysis))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^💬 Feedback$"), handle_feedback))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^⬅️ Назад$"), handle_back))

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_TARGET_CHANNEL in .env.tg")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    register_text_handlers(app)

    app.add_handler(CallbackQueryHandler(back_to_main_cb, pattern="^back_to_main$"))
    app.add_handler(CallbackQueryHandler(open_signal_cb, pattern="^open_signal$"))
    app.add_handler(CallbackQueryHandler(run_full_cb, pattern="^run_full$"))
    app.add_handler(CallbackQueryHandler(signal_pair_cb, pattern=r"^signal_pair:.+"))
    app.add_handler(CallbackQueryHandler(feedback_howto_cb, pattern="^feedback_howto$"))
    app.add_handler(CommandHandler("feedback", feedback_cmd))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_TARGET_CHANNEL in .env.tg")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    register_text_handlers(app)

    app.add_handler(CallbackQueryHandler(back_to_main_cb, pattern="^back_to_main$"))
    app.add_handler(CallbackQueryHandler(open_signal_cb, pattern="^open_signal$"))
    app.add_handler(CallbackQueryHandler(run_full_cb, pattern="^run_full$"))
    app.add_handler(CallbackQueryHandler(signal_pair_cb, pattern=r"^signal_pair:.+"))
    app.add_handler(CallbackQueryHandler(feedback_howto_cb, pattern="^feedback_howto$"))
    app.add_handler(CommandHandler("feedback", feedback_cmd))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

# ================== HANDLERS ===================
from telegram.ext import MessageHandler, filters

async def handle_signal_menu(update, context):
    from telegram import ReplyKeyboardMarkup, KeyboardButton
    kb = [
        [KeyboardButton("🤖 Auto (FULL)")],
        [KeyboardButton("SOL"), KeyboardButton("AVAX"), KeyboardButton("APT")],
        [KeyboardButton("AAVE"), KeyboardButton("LINK"), KeyboardButton("TON")],
        [KeyboardButton("ARB")],
        [KeyboardButton("⬅️ Назад")]
    ]
    await update.message.reply_text("Выбери актив или режим:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_analysis(update, context):
    await update.message.reply_text("Запускаю анализ рынка... это займёт немного времени.")
    import subprocess, re
    out = subprocess.run(["bash", "-lc", "./signal full"], capture_output=True, text=True)
    text = out.stdout[-3500:] or "Нет данных"
    # Экранируем спецсимволы для MarkdownV2
    safe = re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)
    msg = f"```\n{safe}\n```"
    await update.message.reply_text(msg, parse_mode="MarkdownV2")
    await update.message.reply_text("⬅️ Назад", reply_markup=main_menu_kb())

async def handle_back(update, context):
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())

# === Роутинг текстовых кнопок ===

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_TARGET_CHANNEL in .env.tg")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    register_text_handlers(app)

    app.add_handler(CallbackQueryHandler(back_to_main_cb, pattern="^back_to_main$"))
    app.add_handler(CallbackQueryHandler(open_signal_cb, pattern="^open_signal$"))
    app.add_handler(CallbackQueryHandler(run_full_cb, pattern="^run_full$"))
    app.add_handler(CallbackQueryHandler(signal_pair_cb, pattern=r"^signal_pair:.+"))
    app.add_handler(CallbackQueryHandler(feedback_howto_cb, pattern="^feedback_howto$"))
    app.add_handler(CommandHandler("feedback", feedback_cmd))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_TARGET_CHANNEL in .env.tg")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    register_text_handlers(app)

    app.add_handler(CallbackQueryHandler(back_to_main_cb, pattern="^back_to_main$"))
    app.add_handler(CallbackQueryHandler(open_signal_cb, pattern="^open_signal$"))
    app.add_handler(CallbackQueryHandler(run_full_cb, pattern="^run_full$"))
    app.add_handler(CallbackQueryHandler(signal_pair_cb, pattern=r"^signal_pair:.+"))
    app.add_handler(CallbackQueryHandler(feedback_howto_cb, pattern="^feedback_howto$"))
    app.add_handler(CommandHandler("feedback", feedback_cmd))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

# ================== HANDLERS ===================
from telegram.ext import MessageHandler, filters

async def handle_signal_menu(update, context):
    from telegram import ReplyKeyboardMarkup, KeyboardButton
    kb = [
        [KeyboardButton("🤖 Auto (FULL)")],
        [KeyboardButton("SOL"), KeyboardButton("AVAX"), KeyboardButton("APT")],
        [KeyboardButton("AAVE"), KeyboardButton("LINK"), KeyboardButton("TON")],
        [KeyboardButton("ARB")],
        [KeyboardButton("⬅️ Назад")]
    ]
    await update.message.reply_text("Выбери актив или режим:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_analysis(update, context):
    await update.message.reply_text("Запускаю анализ рынка... это займёт немного времени.")
    import subprocess, re
    out = subprocess.run(["bash", "-lc", "./signal full"], capture_output=True, text=True)
    text = out.stdout[-3500:] or "Нет данных"
    # Экранируем спецсимволы для MarkdownV2
    safe = re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)
    msg = f"```\n{safe}\n```"
    await update.message.reply_text(msg, parse_mode="MarkdownV2")
    await update.message.reply_text("⬅️ Назад", reply_markup=main_menu_kb())

async def handle_back(update, context):
    await update.message.reply_text("📋 Главное меню", reply_markup=main_menu_kb())

# === Роутинг текстовых кнопок ===
