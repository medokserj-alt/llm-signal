import asyncio
import importlib.util
import sys
import tempfile
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
    def _proc_with_artifacts(self, *, sig_path: Path, run_log: Path):
        stdout = "\n".join(
            (
                f"✅ Saved logs: {run_log.relative_to(REPO_ROOT).as_posix()}",
                "✅ Last JSON: logs/last.json",
                f"✅ Signal HTML: {sig_path.relative_to(REPO_ROOT).as_posix()}",
            )
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

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
        sig_path = REPO_ROOT / "signal_20260329_010203.html"
        run_log = REPO_ROOT / "logs" / "signal_20260329_010203.log"
        self.bot.subprocess.run = lambda *args, **kwargs: self._proc_with_artifacts(
            sig_path=sig_path,
            run_log=run_log,
        )
        self.bot.latest = lambda pattern: {
            "analysis_*.md": REPO_ROOT / "analysis_20260329_010203.md",
            "signal_*.html": sig_path,
            "logs/signal_*.log": run_log,
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

    def test_personal_bot_main_publication_targets_follow_channel_routing(self) -> None:
        self.bot.USER_CONFIG = {
            "shared_v3_mixed": {"channels": {"main_chat_id": -1003530482991}},
            "6308066297": {"channels": {"main_chat_id": -1003492385200}},
            "8556231754": {"channels": {"main_chat_id": -1003493070625}},
        }

        self.assertEqual(self.bot.get_main_publication_targets(6308066297), [-1003492385200])
        self.assertEqual(self.bot.get_main_publication_targets(8556231754), [-1003493070625])
        self.assertEqual(self.bot.get_main_publication_targets(999999999), [-1003530482991])

    def test_no_trade_payload_includes_origin_routing_metadata(self) -> None:
        self.bot._read_last_signal_json = lambda: {
            "symbol": "BTCUSDT",
            "direction": "long",
        }

        payload = self.bot._build_no_trade_decision_payload(
            8556231754,
            tg_text="NO_TRADE",
            symbol_hint="BTCUSDT",
            channel_id=-1003493070625,
            origin_chat_id=-1003493070625,
            publish_targets=[-1003493070625],
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["origin_user_id"], 8556231754)
        self.assertEqual(payload["origin_chat_id"], -1003493070625)
        self.assertEqual(payload["publish_targets"], [-1003493070625])

    def test_short_mid_report_is_sent_as_single_message(self) -> None:
        context = _FakeContext()
        self.bot.make_header = lambda title: f"{title} • 25.04.2026 14:51"

        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir)
            (report_dir / "analysis_20260425_145100.md").write_text(
                "1️⃣ Среднесрочный режим 3–7 дней\n\n2️⃣ Главные драйверы",
                encoding="utf-8",
            )
            asyncio.run(self.bot._post_report("mid", "📰", context, -1001, report_dir))

        self.assertEqual(len(context.bot.calls), 2)
        self.assertIn("📰 MID • 25.04.2026 14:51", context.bot.calls[1]["text"])
        self.assertNotIn("appendix", context.bot.calls[1]["text"])

    def test_long_day_report_is_split_into_appendix_messages(self) -> None:
        context = _FakeContext()
        self.bot.make_header = lambda title: f"{title} • 25.04.2026 14:51"
        self.bot.REPORT_TELEGRAM_MAX_LEN = 120
        long_body = "\n\n".join(
            [
                "1️⃣ Режим дня " + ("A" * 45),
                "2️⃣ Кандидаты на сегодня " + ("B" * 45),
                "🗓 Events\n- Event one\n- Event two",
                "Flow / Derivatives Context\n- derivatives-only\n- exchange/stablecoin/tokenomics unavailable",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir)
            (report_dir / "analysis_20260425_145100.md").write_text(long_body, encoding="utf-8")
            asyncio.run(self.bot._post_report("day", "🗓", context, -1001, report_dir))

        self.assertGreaterEqual(len(context.bot.calls), 3)
        report_calls = context.bot.calls[1:]
        self.assertIn("part 1/", report_calls[0]["text"])
        self.assertIn("appendix", report_calls[-1]["text"])
        joined = "\n".join(call["text"] for call in report_calls)
        self.assertIn("Flow / Derivatives Context", joined)


if __name__ == "__main__":
    unittest.main()
