from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

router = Router()

# Главное меню
@router.message(Command("start"))
async def cmd_start(msg: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Signal", callback_data="menu_signal")],
        [InlineKeyboardButton(text="🧠 Анализ", callback_data="menu_analysis")],
    ])
    await msg.answer("👋 Привет! Выберите действие:", reply_markup=kb)

# Меню Signal
@router.callback_query(lambda c: c.data == "menu_signal")
async def menu_signal(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Auto", callback_data="sig_auto")],
        [
            InlineKeyboardButton(text="SOL", callback_data="sig_SOL"),
            InlineKeyboardButton(text="AVAX", callback_data="sig_AVAX"),
            InlineKeyboardButton(text="APT", callback_data="sig_APT"),
        ],
        [
            InlineKeyboardButton(text="AAVE", callback_data="sig_AAVE"),
            InlineKeyboardButton(text="LINK", callback_data="sig_LINK"),
            InlineKeyboardButton(text="TON", callback_data="sig_TON"),
        ],
        [InlineKeyboardButton(text="ARB", callback_data="sig_ARB")],
        [InlineKeyboardButton(text="🔙 Вернуться", callback_data="back_main")],
    ])
    await callback.message.edit_text("⚙️ Выберите режим сигнала:", reply_markup=kb)
    await callback.answer()

# Меню Анализ
@router.callback_query(lambda c: c.data == "menu_analysis")
async def menu_analysis(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Full Market", callback_data="ana_full")],
        [InlineKeyboardButton(text="💬 По валюте", callback_data="ana_symbol")],
        [InlineKeyboardButton(text="🔙 Вернуться", callback_data="back_main")],
    ])
    await callback.message.edit_text("📊 Выберите тип анализа:", reply_markup=kb)
    await callback.answer()

# Возврат в главное меню
@router.callback_query(lambda c: c.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Signal", callback_data="menu_signal")],
        [InlineKeyboardButton(text="🧠 Анализ", callback_data="menu_analysis")],
    ])
    await callback.message.edit_text("🔙 Главное меню:", reply_markup=kb)
    await callback.answer()
