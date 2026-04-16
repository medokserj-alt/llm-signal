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

    def test_wait_confirm_payload_caps_timeout_to_three_hours(self) -> None:
        tg_bot = _load_tg_bot_module()
        tg_bot._AIA_UID_CONTEXT = None
        tg_bot._read_last_signal_json = lambda: {
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_range": [613.0, 618.0],
            "sl": 609.35,
            "tp1": 625.0,
            "tp2": 634.0,
            "mode": "aggressive",
            "entry_mode": "wait_confirm",
            "entry_price_aggressive": 615.5,
            "sl_by_mode": {"aggressive": 609.35},
            "tp_by_mode": {"aggressive": {"tvh1": 625.0, "tvh2": 634.0}},
            "max_valid_minutes": 1440,
            "validity_minutes": 720,
            "confirmation_rules": "Вход только после удержания зоны entry_range и ретеста.",
        }

        out = tg_bot._build_signal_json_v1(
            signal_id="sig-wait-cap-1",
            published_at="2026-04-15T09:52:00Z",
            channel_id=-1001234567890,
        )

        self.assertIsNotNone(out)
        self.assertEqual(out["meta"]["entry_type"], "wait_confirm")
        self.assertEqual(out["meta"]["max_wait_minutes"], 180)
        self.assertEqual(out["meta"]["confirm_timeout_minutes"], 180)
        self.assertIn({"type": "close_in_entry_zone"}, out["meta"]["confirm_rule_v1"]["rules"])
        self.assertIn({"type": "retest_entry_zone", "required": True}, out["meta"]["confirm_rule_v1"]["rules"])
        self.assertIn({"type": "deadline_minutes", "value": 180}, out["meta"]["confirm_rule_v1"]["rules"])

    def test_wait_confirm_parser_supports_hold_retest_session_and_event_window_patterns(self) -> None:
        tg_bot = _load_tg_bot_module()

        rule = tg_bot._build_confirm_rule_v1(
            {
                "direction": "long",
                "confirmation_rules": (
                    "Вход только после удержания зоны entry_range и ретеста после прокола. "
                    "Подтверждение в EU/US сессии. "
                    "Не открывать новую позицию в пределах окна ±60 минут до/после US PPI."
                ),
            },
            max_wait_minutes=180,
        )

        self.assertEqual(rule["version"], 1)
        self.assertIn({"type": "close_in_entry_zone"}, rule["rules"])
        self.assertIn({"type": "retest_entry_zone", "required": True}, rule["rules"])
        self.assertIn({"type": "session_gate", "allowed_sessions": ["eu", "us"]}, rule["rules"])
        self.assertIn(
            {"type": "event_window_clear", "min_minutes": 60, "max_event_risk": "low"},
            rule["rules"],
        )

    def test_wait_confirm_parser_supports_reclaim_and_impulse_patterns(self) -> None:
        tg_bot = _load_tg_bot_module()

        rule = tg_bot._build_confirm_rule_v1(
            {
                "direction": "short",
                "confirmation_rules": (
                    "После касания зоны нужен возврат под верхнюю границу диапазона и импульс вниз "
                    "как подтверждение продавца."
                ),
            },
            max_wait_minutes=120,
        )

        self.assertIn({"type": "reclaim_entry_zone", "side": "short"}, rule["rules"])
        self.assertIn({"type": "m15_impulse_in_direction", "side": "short"}, rule["rules"])
        self.assertIn({"type": "deadline_minutes", "value": 120}, rule["rules"])

    def test_wait_confirm_parser_keeps_legacy_supported_rules_backward_compatible(self) -> None:
        tg_bot = _load_tg_bot_module()

        rule = tg_bot._build_confirm_rule_v1(
            {
                "direction": "long",
                "confirmation_rules": (
                    "M15 EMA20: закрытие не ниже EMA20; объём M15 выше среднего 20; "
                    "тенью зайти внутрь зоны."
                ),
            },
            max_wait_minutes=90,
        )

        self.assertIn({"type": "m15_close_vs_ema20", "op": "above"}, rule["rules"])
        self.assertIn({"type": "volume_m15_vs_avg20", "op": ">="}, rule["rules"])
        self.assertIn({"type": "wick_into_entry_zone", "required": True}, rule["rules"])
        self.assertIn({"type": "deadline_minutes", "value": 90}, rule["rules"])


if __name__ == "__main__":
    unittest.main()
