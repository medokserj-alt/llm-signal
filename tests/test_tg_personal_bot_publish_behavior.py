import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TG_PERSONAL_BOT_PATH = REPO_ROOT / "tg_personal_bot.py"


def _load_personal_bot_module():
    saved = {
        "dotenv": sys.modules.get("dotenv"),
        "telegram": sys.modules.get("telegram"),
        "telegram.constants": sys.modules.get("telegram.constants"),
        "telegram.ext": sys.modules.get("telegram.ext"),
    }

    dotenv_mod = types.ModuleType("dotenv")
    dotenv_mod.load_dotenv = lambda *args, **kwargs: False

    telegram_mod = types.ModuleType("telegram")

    class _Dummy:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    telegram_mod.Update = _Dummy
    telegram_mod.KeyboardButton = _Dummy
    telegram_mod.ReplyKeyboardMarkup = _Dummy

    telegram_constants_mod = types.ModuleType("telegram.constants")

    class _ParseMode:
        HTML = "HTML"

    telegram_constants_mod.ParseMode = _ParseMode

    telegram_ext_mod = types.ModuleType("telegram.ext")

    class _Application:
        pass

    class _Handler:
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

    telegram_ext_mod.Application = _Application
    telegram_ext_mod.CommandHandler = _Handler
    telegram_ext_mod.MessageHandler = _Handler
    telegram_ext_mod.ContextTypes = _ContextTypes
    telegram_ext_mod.filters = _Filters

    sys.modules["dotenv"] = dotenv_mod
    sys.modules["telegram"] = telegram_mod
    sys.modules["telegram.constants"] = telegram_constants_mod
    sys.modules["telegram.ext"] = telegram_ext_mod

    sys.modules.pop("tg_personal_bot_test_module", None)
    spec = importlib.util.spec_from_file_location("tg_personal_bot_test_module", TG_PERSONAL_BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tg_personal_bot_test_module"] = module
    try:
        assert spec and spec.loader
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


class _FakeStatusMessage:
    def __init__(self, text: str):
        self.text = text
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edits.append((text, kwargs))


class _FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, reply_markup=None):
        self.calls.append((text, reply_markup))
        return _FakeStatusMessage(text)


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid


class _FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, uid: int, *, text: str = ""):
        self.effective_user = _FakeUser(uid)
        self.effective_chat = _FakeChat(uid)
        self.message = _FakeMessage(text)


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.calls))


class _FakeContext:
    def __init__(self):
        self.bot = _FakeBot()


class TestTgPersonalBotPublishBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = _load_personal_bot_module()
        self.bot.is_allowed = lambda uid: True
        self.bot.can_request = lambda uid: (True, 0)
        self.bot.is_gen_locked = lambda: False
        self.bot.acquire_gen_lock = lambda: True
        self.bot.release_gen_lock = lambda: None
        self.bot.touch_request = lambda uid: None
        self.bot.get_main_chat_id = lambda uid: -1003492385200
        self.bot.get_signal_targets = lambda uid: [-1003492385200]
        self.bot.set_params_mode = lambda mode: None
        self.bot.get_user_mode = lambda uid: "neutral"
        self.bot.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=0)
        self.bot.latest = lambda pattern: {
            "analysis_*.md": REPO_ROOT / "analysis_20260329_010203.md",
            "signal_*.html": REPO_ROOT / "signal_20260329_010203.html",
            "logs/signal_*.log": REPO_ROOT / "logs" / "signal_20260329_010203.log",
        }.get(pattern)
        self.bot._should_send_to_aia_for_target = lambda target: False

    def test_full_mode_publishes_only_final_card(self) -> None:
        update = _FakeUpdate(6308066297)
        context = _FakeContext()
        self.bot.html_file_to_tg_text = lambda path: ["📌 Сигнал не выдан\n\nwait"]

        asyncio.run(self.bot.handle_full(update, context))

        self.assertEqual(len(context.bot.calls), 1)
        self.assertIn("📌 Сигнал не выдан", context.bot.calls[0]["text"])
        self.assertNotIn("LLM Full анализ", context.bot.calls[0]["text"])

    def test_single_mode_publishes_only_final_card(self) -> None:
        update = _FakeUpdate(6308066297, text="SOL")
        context = _FakeContext()
        self.bot.html_file_to_tg_text = lambda path: ["SOL/USDT signal body"]
        self.bot._read_last_signal_json = lambda: {
            "symbol": "SOL/USDT",
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }

        asyncio.run(self.bot.handle_symbol(update, context))

        self.assertEqual(len(context.bot.calls), 1)
        self.assertIn("SOL/USDT signal body", context.bot.calls[0]["text"])
        self.assertNotIn("📝 Анализ SOL/USDT", context.bot.calls[0]["text"])

    def test_single_mode_keeps_single_signal_header_from_renderer(self) -> None:
        update = _FakeUpdate(6308066297, text="SOL")
        context = _FakeContext()
        rendered = "📣 Сигнал\n🕗 Время (МСК): 12:00\n💰 Текущая цена: 100\n📊 Актив: SOL/USDT"
        self.bot.html_file_to_tg_text = lambda path: [rendered]
        self.bot._read_last_signal_json = lambda: {
            "symbol": "SOL/USDT",
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }

        asyncio.run(self.bot.handle_symbol(update, context))

        self.assertEqual(len(context.bot.calls), 1)
        self.assertEqual(context.bot.calls[0]["text"], rendered)
        self.assertEqual(context.bot.calls[0]["text"].count("📣 Сигнал"), 1)

    def test_personal_bot_pool_is_fixed_to_agreed_five_assets(self) -> None:
        self.assertEqual(self.bot.SYMBOLS, ["BTC", "ETH", "BNB", "SOL", "XRP"])


if __name__ == "__main__":
    unittest.main()
