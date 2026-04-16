import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
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
    telegram_constants_mod.ParseMode = type("_ParseMode", (), {"HTML": "HTML"})

    class _DummyFilter:
        def __and__(self, other):
            return self

        def __rand__(self, other):
            return self

        def __invert__(self):
            return self

    telegram_ext_mod = types.ModuleType("telegram.ext")
    telegram_ext_mod.Application = _Dummy
    telegram_ext_mod.CommandHandler = _Dummy
    telegram_ext_mod.MessageHandler = _Dummy
    telegram_ext_mod.CallbackQueryHandler = _Dummy
    telegram_ext_mod.ContextTypes = type("_ContextTypes", (), {"DEFAULT_TYPE": object})
    telegram_ext_mod.filters = type(
        "_Filters",
        (),
        {
            "TEXT": _DummyFilter(),
            "COMMAND": _DummyFilter(),
            "Regex": staticmethod(lambda _pattern: _DummyFilter()),
        },
    )

    sys.modules["dotenv"] = dotenv_mod
    sys.modules["telegram"] = telegram_mod
    sys.modules["telegram.constants"] = telegram_constants_mod
    sys.modules["telegram.ext"] = telegram_ext_mod
    sys.path.insert(0, str(REPO_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        if sys.path and sys.path[0] == str(REPO_ROOT):
            sys.path.pop(0)
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class TestSignalIdTimezoneStandard(unittest.TestCase):
    def test_tg_bot_fallback_signal_id_uses_msk(self) -> None:
        tg_bot = _load_module("tg_bot_test_signal_id", REPO_ROOT / "tg_bot.py")
        self.assertEqual(
            tg_bot._infer_signal_id(None, None, "2026-04-15T21:30:00Z"),
            "20260416_003000",
        )

    def test_tg_personal_bot_fallback_signal_id_uses_msk(self) -> None:
        tg_personal_bot = _load_module("tg_personal_bot_test_signal_id", REPO_ROOT / "tg_personal_bot.py")
        self.assertEqual(
            tg_personal_bot._infer_signal_id(None, None, "2026-04-15T21:30:00Z"),
            "20260416_003000",
        )


if __name__ == "__main__":
    unittest.main()
