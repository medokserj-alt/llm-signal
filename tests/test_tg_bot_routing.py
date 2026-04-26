import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TG_BOT_PATH = REPO_ROOT / "tg_bot.py"
TG_SIGNAL_BOT_PATH = REPO_ROOT / "tg_signal_bot.py"


def _load_module(module_path: Path, module_name: str):
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

    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
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


def _load_tg_bot_module():
    return _load_module(TG_BOT_PATH, "tg_bot_test_module")


def _load_tg_signal_bot_module():
    return _load_module(TG_SIGNAL_BOT_PATH, "tg_signal_bot_test_module")


class _FakeMessage:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text, reply_markup=None):
        self.calls.append((text, reply_markup))


class _FakeUser:
    def __init__(self, uid: int, first_name: str = "Enzo"):
        self.id = uid
        self.first_name = first_name


class _FakeChat:
    def __init__(self, chat_type: str = "private", chat_id: int = 1):
        self.type = chat_type
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, uid: int, chat_type: str = "private"):
        self.effective_user = _FakeUser(uid)
        self.effective_chat = _FakeChat(chat_type=chat_type, chat_id=uid)
        self.message = _FakeMessage()
        self.effective_message = self.message


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.calls))


class _FakeContext:
    def __init__(self):
        self.bot = _FakeBot()


class TestTgBotRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tg_bot = _load_tg_bot_module()
        self.tg_bot.USER_CONFIG = {
            "shared_v3_mixed": {
                "channels": {
                    "main_chat_id": -1003530482991,
                }
            },
            "6308066297": {
                "channels": {
                    "main_chat_id": -1003492385200,
                }
            },
            "672885732": {
                "channels": {
                    "main_chat_id": -1003493070625,
                }
            },
            "177651027": {
                "route": "shared_v3_mixed",
            },
        }
        self.tg_bot.SUBSCRIBERS = {82052103, 177651027, 5278300959, 7879055214}

    def _proc_with_artifacts(self, *, sig_path: Path, run_log: Path):
        stdout = "\n".join(
            (
                f"✅ Saved logs: {run_log.relative_to(REPO_ROOT).as_posix()}",
                "✅ Last JSON: logs/last.json",
                f"✅ Signal HTML: {sig_path.relative_to(REPO_ROOT).as_posix()}",
            )
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def _run_symbol_publish(self, uid: int, *, delivery_kind: str = "main", symbol: str = "BTC/USDT"):
        context = _FakeContext()
        signal_events = []
        personal_calls = []
        aia_signal_calls = []
        aia_no_trade_calls = []

        self.tg_bot.set_params_mode = lambda mode: None
        sig_path = REPO_ROOT / "signal_20260329_010203.html"
        run_log = REPO_ROOT / "logs" / "signal_20260329_010203.log"
        self.tg_bot.subprocess.run = lambda *args, **kwargs: self._proc_with_artifacts(
            sig_path=sig_path,
            run_log=run_log,
        )
        self.tg_bot.latest = lambda pattern: {
            "analysis_*.md": None,
            "signal_*.html": sig_path,
            "logs/signal_*.log": run_log,
        }.get(pattern)
        self.tg_bot.html_file_to_tg_text = lambda path: [f"{symbol} signal body"]
        self.tg_bot._read_last_signal_json = lambda: {
            "symbol": symbol,
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }
        self.tg_bot._log_signal_publication = lambda **kwargs: signal_events.append(kwargs)
        self.tg_bot._queue_aia_signal_forward = lambda payload, **kwargs: aia_signal_calls.append((payload, kwargs))
        self.tg_bot._queue_aia_no_trade_forward = lambda payload, **kwargs: aia_no_trade_calls.append((payload, kwargs))
        self.tg_bot._send_personal = lambda target_uid, text, **kwargs: personal_calls.append(
            {"uid": target_uid, "text": text, **kwargs}
        ) or True

        asyncio.run(
            self.tg_bot._run_symbol_core(
                uid,
                symbol,
                context,
                {"status": "paid"},
                delivery_kind=delivery_kind,
            )
        )

        return {
            "context": context,
            "signal_events": signal_events,
            "personal_calls": personal_calls,
            "aia_signal_calls": aia_signal_calls,
            "aia_no_trade_calls": aia_no_trade_calls,
        }

    def _run_full_publish(self, uid: int, *, delivery_kind: str = "main", first_part: str = "BTC/USDT signal body"):
        context = _FakeContext()
        signal_events = []
        personal_calls = []
        aia_signal_calls = []
        aia_no_trade_calls = []

        self.tg_bot.set_params_mode = lambda mode: None
        analysis_path = REPO_ROOT / "analysis_20260329_010203.md"
        sig_path = REPO_ROOT / "signal_20260329_010203.html"
        run_log = REPO_ROOT / "logs" / "signal_20260329_010203.log"
        self.tg_bot.subprocess.run = lambda *args, **kwargs: self._proc_with_artifacts(
            sig_path=sig_path,
            run_log=run_log,
        )
        self.tg_bot.latest = lambda pattern: {
            "analysis_*.md": analysis_path,
            "signal_*.html": sig_path,
            "logs/signal_*.log": run_log,
        }.get(pattern)
        self.tg_bot.html_file_to_tg_text = lambda path: [first_part]
        self.tg_bot._read_last_signal_json = lambda: {
            "symbol": "BTC/USDT",
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }
        self.tg_bot._log_signal_publication = lambda **kwargs: signal_events.append(kwargs)
        self.tg_bot._queue_aia_signal_forward = lambda payload, **kwargs: aia_signal_calls.append((payload, kwargs))
        self.tg_bot._queue_aia_no_trade_forward = lambda payload, **kwargs: aia_no_trade_calls.append((payload, kwargs))
        self.tg_bot._send_personal = lambda target_uid, text, **kwargs: personal_calls.append(
            {"uid": target_uid, "text": text, **kwargs}
        ) or True

        asyncio.run(
            self.tg_bot._run_full_core(
                uid,
                context,
                {"status": "paid"},
                delivery_kind=delivery_kind,
            )
        )

        return {
            "context": context,
            "signal_events": signal_events,
            "personal_calls": personal_calls,
            "aia_signal_calls": aia_signal_calls,
            "aia_no_trade_calls": aia_no_trade_calls,
        }

    def test_enzo_routes_only_to_dedicated_channel(self) -> None:
        self.assertEqual(
            self.tg_bot.get_main_publication_targets(6308066297),
            [-1003492385200],
        )

    def test_dima_routes_only_to_dedicated_channel(self) -> None:
        self.assertEqual(
            self.tg_bot.get_main_publication_targets(672885732),
            [-1003493070625],
        )

    def test_shared_route_uid_uses_only_shared_channel(self) -> None:
        self.assertEqual(
            self.tg_bot.get_main_publication_targets(177651027),
            [-1003530482991],
        )

    def test_subscribers_do_not_affect_main_signal_routing(self) -> None:
        self.tg_bot._get_core_user_targets = lambda: [6308066297, 672885732, 177651027]
        self.assertEqual(
            self.tg_bot.get_core_broadcast_targets(),
            [-1003530482991, -1003493070625, -1003492385200],
        )
        self.tg_bot.SUBSCRIBERS.update({6308066297, 672885732, 999999999})
        self.assertEqual(
            self.tg_bot.get_core_broadcast_targets(),
            [-1003530482991, -1003493070625, -1003492385200],
        )

    def test_start_private_chat_shows_menu_without_signal_bot_hint(self) -> None:
        update = _FakeUpdate(6308066297, chat_type="private")
        self.tg_bot.register_user = lambda user: ({"status": "paid"}, False)
        self.tg_bot.status_text = lambda record: "Статус: paid"
        self.tg_bot.is_allowed = lambda uid: True

        async def _unexpected_hint(*args, **kwargs):
            raise AssertionError("signal bot hint should not be used for private /start")

        self.tg_bot._send_signal_bot_hint = _unexpected_hint

        asyncio.run(self.tg_bot.start(update, object()))

        texts = [text for text, _ in update.message.calls]
        self.assertIn("Привет, Enzo!", texts)
        self.assertIn("Статус: paid", texts)
        self.assertIn("📋 Главное меню", texts)

    def test_enzo_main_signal_publishes_once_to_dedicated_channel(self) -> None:
        result = self._run_symbol_publish(6308066297)
        self.assertEqual(
            [call["chat_id"] for call in result["context"].bot.calls],
            [-1003492385200],
        )
        self.assertEqual(
            [event["event"] for event in result["signal_events"] if event["event"] == "telegram_publish"],
            ["telegram_publish"],
        )
        self.assertEqual(len(result["aia_signal_calls"]), 1)
        self.assertEqual(result["aia_signal_calls"][0][1]["target_chat_id"], -1003492385200)
        self.assertEqual(result["aia_signal_calls"][0][1]["source"], "tg_bot.py:_run_symbol_core")

    def test_dima_main_signal_publishes_once_to_dedicated_channel(self) -> None:
        result = self._run_symbol_publish(672885732, symbol="ETH/USDT")
        self.assertEqual(
            [call["chat_id"] for call in result["context"].bot.calls],
            [-1003493070625],
        )
        self.assertEqual(
            [event["target_chat_id"] for event in result["signal_events"] if event["event"] == "telegram_publish"],
            [-1003493070625],
        )

    def test_shared_route_user_publishes_once_to_shared_channel(self) -> None:
        result = self._run_symbol_publish(177651027, symbol="SOL/USDT")
        self.assertEqual(
            [call["chat_id"] for call in result["context"].bot.calls],
            [-1003530482991],
        )
        self.assertEqual(
            [event["target_chat_id"] for event in result["signal_events"] if event["event"] == "telegram_publish"],
            [-1003530482991],
        )

    def test_post_init_does_not_start_core_watcher_by_default(self) -> None:
        created = []
        original_create_task = self.tg_bot.asyncio.create_task

        def _capture_task(coro):
            created.append(coro)
            coro.close()
            return None

        self.tg_bot.asyncio.create_task = _capture_task
        try:
            asyncio.run(self.tg_bot._post_init(object()))
        finally:
            self.tg_bot.asyncio.create_task = original_create_task

        self.assertEqual(created, [])

    def test_importing_tg_signal_bot_does_not_duplicate_main_publish(self) -> None:
        tg_signal_bot = _load_tg_signal_bot_module()
        self.assertTrue(callable(tg_signal_bot.main))

        result = self._run_symbol_publish(6308066297, symbol="XRP/USDT")

        self.assertEqual(
            [call["chat_id"] for call in result["context"].bot.calls],
            [-1003492385200],
        )
        self.assertEqual(
            len([event for event in result["signal_events"] if event["event"] == "telegram_publish"]),
            1,
        )

    def test_full_mode_skips_separate_llm_full_analysis_publish(self) -> None:
        result = self._run_full_publish(6308066297, first_part="📌 Сигнал не выдан\n\nwait")

        self.assertEqual(len(result["context"].bot.calls), 1)
        self.assertEqual(result["context"].bot.calls[0]["chat_id"], -1003492385200)
        self.assertIn("📌 Сигнал не выдан", result["context"].bot.calls[0]["text"])
        self.assertNotIn("LLM Full анализ", result["context"].bot.calls[0]["text"])

    def test_full_mode_publish_queues_aia_send_only_after_bot_publish_path(self) -> None:
        result = self._run_full_publish(6308066297, first_part="BTC/USDT signal body")

        self.assertEqual(
            [event["event"] for event in result["signal_events"] if event["event"] == "telegram_publish"],
            ["telegram_publish"],
        )
        self.assertEqual(len(result["aia_signal_calls"]), 1)
        payload, meta = result["aia_signal_calls"][0]
        self.assertEqual(meta["target_chat_id"], -1003492385200)
        self.assertEqual(meta["source"], "tg_bot.py:_run_full_core")
        self.assertEqual(payload["symbol"], "BTC/USDT")
        self.assertEqual(payload["channel_id"], -1003492385200)

    def test_single_mode_skips_separate_analysis_header_publish(self) -> None:
        result = self._run_symbol_publish(6308066297, symbol="SOL/USDT")

        self.assertEqual(len(result["context"].bot.calls), 1)
        self.assertEqual(result["context"].bot.calls[0]["chat_id"], -1003492385200)
        self.assertIn("SOL/USDT signal body", result["context"].bot.calls[0]["text"])
        self.assertNotIn("📝 Анализ SOL/USDT", result["context"].bot.calls[0]["text"])

    def test_single_mode_does_not_prepend_duplicate_signal_header(self) -> None:
        context = _FakeContext()
        self.tg_bot.set_params_mode = lambda mode: None
        sig_path = REPO_ROOT / "signal_20260329_010203.html"
        run_log = REPO_ROOT / "logs" / "signal_20260329_010203.log"
        self.tg_bot.subprocess.run = lambda *args, **kwargs: self._proc_with_artifacts(
            sig_path=sig_path,
            run_log=run_log,
        )
        rendered = "📣 Сигнал\n🕗 Время (МСК): 12:00\n💰 Текущая цена: 100\n📊 Актив: SOL/USDT"
        self.tg_bot.latest = lambda pattern: {
            "analysis_*.md": None,
            "signal_*.html": sig_path,
            "logs/signal_*.log": run_log,
        }.get(pattern)
        self.tg_bot.html_file_to_tg_text = lambda path: [rendered]
        self.tg_bot._read_last_signal_json = lambda: {
            "symbol": "SOL/USDT",
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }
        self.tg_bot._log_signal_publication = lambda **kwargs: None
        self.tg_bot._queue_aia_signal_forward = lambda payload, **kwargs: None
        self.tg_bot._queue_aia_no_trade_forward = lambda payload, **kwargs: None
        self.tg_bot._send_personal = lambda *args, **kwargs: True

        asyncio.run(
            self.tg_bot._run_symbol_core(
                6308066297,
                "SOL/USDT",
                context,
                {"status": "paid"},
            )
        )

        self.assertEqual(context.bot.calls[0]["text"], rendered)
        self.assertEqual(context.bot.calls[0]["text"].count("📣 Сигнал"), 1)

    def test_run_symbol_core_uses_explicit_signal_html_from_current_run(self) -> None:
        context = _FakeContext()
        stale_sig = REPO_ROOT / "signal_20260328_235959.html"
        fresh_sig = REPO_ROOT / "signal_20260329_010203.html"
        run_log = REPO_ROOT / "logs" / "signal_20260329_010203.log"
        seen_paths = []

        self.tg_bot.set_params_mode = lambda mode: None
        self.tg_bot.subprocess.run = lambda *args, **kwargs: self._proc_with_artifacts(
            sig_path=fresh_sig,
            run_log=run_log,
        )
        self.tg_bot.latest = lambda pattern: {
            "analysis_*.md": None,
            "signal_*.html": stale_sig,
            "logs/signal_*.log": REPO_ROOT / "logs" / "signal_20260328_235959.log",
        }.get(pattern)
        self.tg_bot.html_file_to_tg_text = lambda path: seen_paths.append(path) or ["fresh body"]
        self.tg_bot._read_last_signal_json = lambda: {
            "symbol": "SOL/USDT",
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }
        self.tg_bot._log_signal_publication = lambda **kwargs: None
        self.tg_bot._queue_aia_signal_forward = lambda payload, **kwargs: None
        self.tg_bot._queue_aia_no_trade_forward = lambda payload, **kwargs: None
        self.tg_bot._send_personal = lambda *args, **kwargs: True

        asyncio.run(
            self.tg_bot._run_symbol_core(
                6308066297,
                "SOL/USDT",
                context,
                {"status": "paid"},
            )
        )

        self.assertEqual(seen_paths, [fresh_sig])
        self.assertEqual(context.bot.calls[0]["text"], "fresh body")

    def test_run_full_core_uses_explicit_signal_html_from_current_run(self) -> None:
        context = _FakeContext()
        stale_sig = REPO_ROOT / "signal_20260328_235959.html"
        fresh_sig = REPO_ROOT / "signal_20260329_010203.html"
        run_log = REPO_ROOT / "logs" / "signal_20260329_010203.log"
        seen_paths = []

        self.tg_bot.set_params_mode = lambda mode: None
        self.tg_bot.subprocess.run = lambda *args, **kwargs: self._proc_with_artifacts(
            sig_path=fresh_sig,
            run_log=run_log,
        )
        self.tg_bot.latest = lambda pattern: {
            "analysis_*.md": REPO_ROOT / "analysis_20260328_235959.md",
            "signal_*.html": stale_sig,
            "logs/signal_*.log": REPO_ROOT / "logs" / "signal_20260328_235959.log",
        }.get(pattern)
        self.tg_bot.html_file_to_tg_text = lambda path: seen_paths.append(path) or ["fresh full body"]
        self.tg_bot._read_last_signal_json = lambda: {
            "symbol": "BTC/USDT",
            "direction": "long",
            "entry_range": {"min": 1.0, "max": 2.0},
            "sl": 0.5,
            "tp1": 3.0,
            "tp2": 4.0,
        }
        self.tg_bot._log_signal_publication = lambda **kwargs: None
        self.tg_bot._queue_aia_signal_forward = lambda payload, **kwargs: None
        self.tg_bot._queue_aia_no_trade_forward = lambda payload, **kwargs: None
        self.tg_bot._send_personal = lambda *args, **kwargs: True

        asyncio.run(
            self.tg_bot._run_full_core(
                6308066297,
                context,
                {"status": "paid"},
            )
        )

        self.assertEqual(seen_paths, [fresh_sig])
        self.assertEqual(context.bot.calls[0]["text"], "fresh full body")

    def test_personal_delivery_does_not_duplicate_main_channel_publish(self) -> None:
        result = self._run_symbol_publish(6308066297, delivery_kind="personal", symbol="TON/USDT")

        self.assertEqual(result["context"].bot.calls, [])
        self.assertEqual(
            [call["uid"] for call in result["personal_calls"]],
            [6308066297],
        )
        self.assertEqual(
            [event["delivery_kind"] for event in result["signal_events"] if event["event"] == "telegram_publish"],
            ["personal"],
        )

    def test_bot_pool_is_fixed_to_agreed_five_assets(self) -> None:
        self.assertEqual(self.tg_bot.SYMBOLS, ["BTC", "ETH", "BNB", "SOL", "XRP"])

    def test_short_day_report_is_sent_as_single_message(self) -> None:
        context = _FakeContext()
        self.tg_bot.make_header = lambda title: f"{title} • 25.04.2026 14:51"

        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir)
            (report_dir / "analysis_20260425_145100.md").write_text(
                "1️⃣ Режим дня\n\n2️⃣ Кандидаты на сегодня",
                encoding="utf-8",
            )
            asyncio.run(self.tg_bot._post_report("day", "🗓", context, -1001, report_dir))

        self.assertEqual(len(context.bot.calls), 2)
        self.assertIn("🗓 DAY • 25.04.2026 14:51", context.bot.calls[1]["text"])
        self.assertNotIn("part 1/2", context.bot.calls[1]["text"])

    def test_long_mid_report_is_split_into_appendix_messages(self) -> None:
        context = _FakeContext()
        self.tg_bot.make_header = lambda title: f"{title} • 25.04.2026 14:51"
        self.tg_bot.REPORT_TELEGRAM_MAX_LEN = 120
        long_body = "\n\n".join(
            [
                "1️⃣ Среднесрочный режим 3–7 дней " + ("A" * 40),
                "2️⃣ Главные драйверы " + ("B" * 40),
                "🗓 Events\n- Event one\n- Event two",
                "⚡ Regime catalysts\n- Catalyst one\n- Catalyst two",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir)
            (report_dir / "analysis_20260425_145100.md").write_text(long_body, encoding="utf-8")
            asyncio.run(self.tg_bot._post_report("mid", "📰", context, -1001, report_dir))

        self.assertGreaterEqual(len(context.bot.calls), 3)
        report_calls = context.bot.calls[1:]
        self.assertIn("part 1/", report_calls[0]["text"])
        self.assertIn("appendix", report_calls[-1]["text"])
        self.assertTrue(all(len(call["text"]) <= 160 for call in report_calls))
        joined = "\n".join(call["text"] for call in report_calls)
        self.assertIn("⚡ Regime catalysts", joined)

    def test_report_splitter_uses_section_boundaries(self) -> None:
        self.tg_bot.REPORT_TELEGRAM_MAX_LEN = 80
        chunks = self.tg_bot._split_text_for_telegram_sections(
            "\n\n".join(
                [
                    "Section Alpha " + ("A" * 20),
                    "Section Beta " + ("B" * 20),
                    "Section Gamma " + ("C" * 20),
                ]
            ),
            max_len=80,
        )

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("Section ") for chunk in chunks))
        self.assertTrue(all(not chunk.endswith(" ") for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
