import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TG_BOT_PATH = REPO_ROOT / "tg_bot.py"


def _load_tg_bot_module():
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
    telegram_mod.InlineKeyboardButton = _Dummy
    telegram_mod.InlineKeyboardMarkup = _Dummy

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
    telegram_ext_mod.CallbackQueryHandler = _Handler
    telegram_ext_mod.ContextTypes = _ContextTypes
    telegram_ext_mod.filters = _Filters

    sys.modules["dotenv"] = dotenv_mod
    sys.modules["telegram"] = telegram_mod
    sys.modules["telegram.constants"] = telegram_constants_mod
    sys.modules["telegram.ext"] = telegram_ext_mod

    module_name = "tg_bot_signal_payload_test_module"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, TG_BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
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


class TestTgBotSignalPayload(unittest.TestCase):
    def test_build_signal_json_for_issued_full_signal_contains_required_contract_fields(self) -> None:
        tg_bot = _load_tg_bot_module()
        tg_bot._AIA_UID_CONTEXT = None
        tg_bot._read_last_signal_json = lambda: {
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_range": [586.0, 592.0],
            "sl": 583.11,
            "tp1": 612.0,
            "tp2": 635.0,
            "mode": "neutral",
            "entry_mode": "pullback",
            "entry_price_neutral": 589.0,
            "sl_by_mode": {"neutral": 583.11},
            "tp_by_mode": {"neutral": {"tvh1": 612.0, "tvh2": 635.0}},
        }

        out = tg_bot._build_signal_json_v1(
            signal_id="sig-full-1",
            published_at="2026-04-05T17:35:00Z",
            channel_id="-1001234567890",
        )

        self.assertIsNotNone(out)
        self.assertEqual(out["signal_id"], "sig-full-1")
        self.assertEqual(out["symbol"], "BNB/USDT")
        self.assertEqual(out["direction"], "long")
        self.assertEqual(out["entry_zone"], [586.0, 592.0])
        self.assertEqual(out["entry_price"], 589.0)
        self.assertEqual(out["sl"], 583.11)
        self.assertEqual(out["tp"], {"tp1": 612.0, "tp2": 635.0})
        self.assertEqual(out["channel_id"], -1001234567890)

    def test_build_signal_json_uses_mode_from_last_json_without_uid_context(self) -> None:
        tg_bot = _load_tg_bot_module()
        tg_bot._AIA_UID_CONTEXT = None
        tg_bot._read_last_signal_json = lambda: {
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_range": [586.0, 592.0],
            "sl": 583.11,
            "tp1": 612.0,
            "tp2": 635.0,
            "mode": "aggressive",
            "entry_mode": "pullback",
            "entry_price_aggressive": 589.0,
            "entry_price_neutral": 588.0,
            "sl_by_mode": {"aggressive": 583.11},
            "tp_by_mode": {"aggressive": {"tvh1": 612.0, "tvh2": 635.0}},
        }

        out = tg_bot._build_signal_json_v1(
            signal_id="sig-1",
            published_at="2026-04-05T17:35:00Z",
            channel_id=-1001234567890,
        )

        self.assertIsNotNone(out)
        self.assertEqual(out["entry_price"], 589.0)
        self.assertEqual(out["channel_id"], -1001234567890)
        self.assertEqual(out["meta"]["mode"], "aggressive")

    def test_build_signal_json_falls_back_to_entry_range_midpoint(self) -> None:
        tg_bot = _load_tg_bot_module()
        tg_bot._AIA_UID_CONTEXT = None
        tg_bot._read_last_signal_json = lambda: {
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_range": [582.0, 592.0],
            "sl": 581.13,
            "tp1": 610.0,
            "tp2": 630.0,
            "mode": "aggressive",
            "entry_mode": "limit_from_base",
        }

        out = tg_bot._build_signal_json_v1(
            signal_id="sig-2",
            published_at="2026-04-05T17:35:00Z",
            channel_id=-1001234567890,
        )

        self.assertIsNotNone(out)
        self.assertEqual(out["entry_price"], 587.0)
        self.assertEqual(out["meta"]["entry_type"], "pullback")


if __name__ == "__main__":
    unittest.main()
