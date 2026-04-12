#!/usr/bin/env python3
from __future__ import annotations

import os

from dotenv import load_dotenv
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from user_registry import get_user, register_user, set_paid, set_signal_bot_started, status_text
from paid_allowlist import add_paid_allowed_uid

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env.tg.clean"))

BOT_TOKEN = os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN")
CORE_BOT_USERNAME = os.getenv("TELEGRAM_CORE_BOT_USERNAME", "V3_bot")
SIGNAL_BOT_USERNAME = os.getenv("TELEGRAM_SIGNAL_BOT_USERNAME", "LLM_signals_pa_dev_bot")


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💳 Оплата")],
            [KeyboardButton("🧾 Статус")],
        ],
        resize_keyboard=True,
    )


def _core_bot_hint() -> str:
    name = (CORE_BOT_USERNAME or "V3_bot").lstrip("@")
    return f"Запросы сигналов делаются в @{name}."


def _signal_bot_name() -> str:
    return (SIGNAL_BOT_USERNAME or "LLM_signals_pa_dev_bot").lstrip("@")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record, _ = register_user(user)
    if record.get("status") not in ("trial", "paid"):
        await update.message.reply_text(
            "Доступ после оплаты или trial. Перейди в V3_bot.",
            reply_markup=main_menu_kb(),
        )
        return
    set_signal_bot_started(user.id, True)
    await update.message.reply_text(
        f"✅ @{_signal_bot_name()} активирован.\n{_core_bot_hint()}",
        reply_markup=main_menu_kb(),
    )


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        add_paid_allowed_uid(uid)
    except Exception:
        pass
    record = set_paid(update.effective_user)
    await update.message.reply_text("💳 Оплата принята (мок). Подписка активна.")
    await update.message.reply_text(status_text(record), reply_markup=main_menu_kb())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    record = get_user(uid)
    if not isinstance(record, dict):
        await update.message.reply_text(
            "Сначала зарегистрируйся в V3_bot: /start", reply_markup=main_menu_kb()
        )
        return
    await update.message.reply_text(status_text(record), reply_markup=main_menu_kb())


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_SIGNAL_BOT_TOKEN in .env.tg.clean")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("subscribe", pay))
    app.add_handler(CommandHandler("help", status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^💳 Оплата$"), pay))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^🧾 Статус$"), status))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
