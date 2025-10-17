#!/usr/bin/env python3
import os, time, subprocess, pathlib, re, html as htmllib
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

BASE = pathlib.Path(__file__).resolve().parent
load_dotenv(BASE/".env.tg")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL   = os.getenv("TELEGRAM_TARGET_CHANNEL")

def parse_allowed_ids() -> list[int]:
    ids = []
    raw_multi = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    raw_legacy = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    for raw in (raw_multi, raw_legacy):
        for x in raw.replace(";", ",").split(","):
            x = x.strip()
            if x and (x.lstrip("-").isdigit()):
                try:
                    ids.append(int(x))
                except:  # noqa
                    pass
    # убираем дубли
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
    # если список пуст — блокируем всех (явная безопасность)
    return (uid in ALLOWED_UIDS) if ALLOWED_UIDS else False

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
    kb = [[InlineKeyboardButton("🔍 FULL анализ (пул)", callback_data="run_full")]]
    await update.message.reply_text("Выбери действие:", reply_markup=InlineKeyboardMarkup(kb))

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

def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_TARGET_CHANNEL in .env.tg")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(run_full_cb, pattern="^run_full$"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
