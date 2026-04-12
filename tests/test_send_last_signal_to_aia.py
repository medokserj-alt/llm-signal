import importlib.util
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "send_last_signal_to_aia.py"


def _load_module():
    saved_tg_bot = sys.modules.get("tg_bot")

    tg_bot_mod = types.ModuleType("tg_bot")
    tg_bot_mod.ALLOWED_UIDS = []
    tg_bot_mod.FALLBACK_CHANNEL = None
    tg_bot_mod.PAID_ALLOWED_UIDS = []
    tg_bot_mod._AIA_TEST_CHANNEL_ID_INT = None
    tg_bot_mod._build_signal_json_v1 = lambda **kwargs: None
    tg_bot_mod._infer_signal_id = lambda *args, **kwargs: "sig-1"
    tg_bot_mod._read_last_signal_json = lambda: {}
    tg_bot_mod._should_send_to_aia_for_target = lambda channel_id: True
    tg_bot_mod._utc_now_z = lambda: "2026-04-05T17:35:00Z"
    tg_bot_mod.get_all_users = lambda: {}
    tg_bot_mod.get_main_publication_chat_id = lambda uid: None
    tg_bot_mod.is_active = lambda record: True
    tg_bot_mod.send_signal_to_aia = lambda payload: True

    sys.modules["tg_bot"] = tg_bot_mod
    sys.modules.pop("send_last_signal_to_aia_test_module", None)
    spec = importlib.util.spec_from_file_location("send_last_signal_to_aia_test_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["send_last_signal_to_aia_test_module"] = module
    try:
        assert spec and spec.loader
        spec.loader.exec_module(module)
    finally:
        if saved_tg_bot is None:
            sys.modules.pop("tg_bot", None)
        else:
            sys.modules["tg_bot"] = saved_tg_bot
    return module


class TestSendLastSignalToAia(unittest.TestCase):
    def test_persist_final_signal_payload_updates_last_json(self) -> None:
        module = _load_module()
        payload = {
            "signal_id": "sig-1",
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_zone": [586.0, 592.0],
            "entry_price": 589.0,
            "sl": 583.11,
            "tp": {"tp1": 612.0, "tp2": 635.0},
            "published_at": "2026-04-05T17:35:00Z",
            "channel_id": -1001234567890,
        }

        with TemporaryDirectory() as td:
            last_json = Path(td) / "last.json"
            last_json.write_text(
                json.dumps(
                    {
                        "entry_price": None,
                        "published_at": None,
                        "sl": None,
                        "tp1": None,
                        "tp2": None,
                        "signal_json_v1": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            module.LAST_JSON_PATH = last_json

            module._persist_final_signal_payload(payload)

            out = json.loads(last_json.read_text(encoding="utf-8"))
            self.assertEqual(out["entry_price"], 589.0)
            self.assertEqual(out["published_at"], "2026-04-05T17:35:00Z")
            self.assertEqual(out["sl"], 583.11)
            self.assertEqual(out["tp1"], 612.0)
            self.assertEqual(out["tp2"], 635.0)
            self.assertEqual(out["signal_id"], "sig-1")
            self.assertEqual(out["channel_id"], -1001234567890)
            self.assertEqual(out["signal_json_v1"], payload)

    def test_main_reports_ok_and_sends_full_payload_contract(self) -> None:
        module = _load_module()
        payload = {
            "signal_id": "sig-1",
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_zone": [586.0, 592.0],
            "entry_price": 589.0,
            "sl": 583.11,
            "tp": {"tp1": 612.0, "tp2": 635.0},
            "published_at": "2026-04-05T17:35:00Z",
            "channel_id": -1001234567890,
        }

        with TemporaryDirectory() as td:
            last_json = Path(td) / "last.json"
            last_json.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")

            module.LAST_JSON_PATH = last_json
            module._build_signal_json_v1 = lambda **kwargs: payload
            module._resolve_channel_id = lambda: -1001234567890
            module._should_send_to_aia_for_target = lambda channel_id: True
            send_calls = []
            module.send_signal_to_aia = lambda body: send_calls.append(body) or True

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = module.main()

            self.assertEqual(rc, 0)
            self.assertEqual(send_calls, [payload])
            self.assertIn("send_signal_to_aia(full): OK", buf.getvalue())
            sent = send_calls[0]
            self.assertEqual(sent["signal_id"], "sig-1")
            self.assertEqual(sent["symbol"], "BNB/USDT")
            self.assertEqual(sent["direction"], "long")
            self.assertEqual(sent["entry_zone"], [586.0, 592.0])
            self.assertEqual(sent["sl"], 583.11)
            self.assertEqual(sent["tp"], {"tp1": 612.0, "tp2": 635.0})
            self.assertEqual(sent["channel_id"], -1001234567890)

    def test_main_reports_channel_id_missing_and_still_persists_payload(self) -> None:
        module = _load_module()
        payload = {
            "signal_id": "sig-1",
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_zone": [586.0, 592.0],
            "entry_price": 589.0,
            "sl": 583.11,
            "tp": {"tp1": 612.0, "tp2": 635.0},
            "published_at": "2026-04-05T17:35:00Z",
            "channel_id": None,
        }

        with TemporaryDirectory() as td:
            last_json = Path(td) / "last.json"
            last_json.write_text(
                json.dumps({"entry_price": None, "published_at": None}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            module.LAST_JSON_PATH = last_json
            module._build_signal_json_v1 = lambda **kwargs: payload
            module._resolve_channel_id = lambda: None
            send_calls = []
            module.send_signal_to_aia = lambda body: send_calls.append(body) or True

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = module.main()

            out = json.loads(last_json.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(out["entry_price"], 589.0)
            self.assertEqual(out["published_at"], "2026-04-05T17:35:00Z")
            self.assertEqual(out["signal_json_v1"], payload)
            self.assertEqual(send_calls, [])
            self.assertIn("send_signal_to_aia(full): FAIL (channel_id_missing)", buf.getvalue())

    def test_main_reports_target_gated_without_attempting_send(self) -> None:
        module = _load_module()
        payload = {
            "signal_id": "sig-1",
            "symbol": "BNB/USDT",
            "direction": "long",
            "entry_zone": [586.0, 592.0],
            "entry_price": 589.0,
            "sl": 583.11,
            "tp": {"tp1": 612.0, "tp2": 635.0},
            "published_at": "2026-04-05T17:35:00Z",
            "channel_id": -1001234567890,
        }

        with TemporaryDirectory() as td:
            last_json = Path(td) / "last.json"
            last_json.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")

            module.LAST_JSON_PATH = last_json
            module._build_signal_json_v1 = lambda **kwargs: payload
            module._resolve_channel_id = lambda: -1001234567890
            module._should_send_to_aia_for_target = lambda channel_id: False
            send_calls = []
            module.send_signal_to_aia = lambda body: send_calls.append(body) or True

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = module.main()

            self.assertEqual(rc, 0)
            self.assertEqual(send_calls, [])
            self.assertIn("send_signal_to_aia(full): SKIP (target_gated channel_id=-1001234567890)", buf.getvalue())

    def test_resolve_channel_id_uses_most_recent_active_main_chat(self) -> None:
        module = _load_module()
        module.FALLBACK_CHANNEL = None
        module._AIA_TEST_CHANNEL_ID_INT = None
        module._read_last_signal_json = lambda: {}
        module.ALLOWED_UIDS = []
        module.PAID_ALLOWED_UIDS = []
        module.get_all_users = lambda: {
            "1": {"status": "paid", "updated_at": "2026-04-05T17:35:00Z"},
            "2": {"status": "paid", "updated_at": "2026-04-05T17:40:00Z"},
        }
        module.is_active = lambda record: True
        module.get_main_publication_chat_id = lambda uid: {1: -100111, 2: -100222}.get(uid)

        old = os.environ.pop("SIGNAL_AIA_CHANNEL_ID", None)
        try:
            self.assertEqual(module._resolve_channel_id(), -100222)
        finally:
            if old is not None:
                os.environ["SIGNAL_AIA_CHANNEL_ID"] = old

    def test_main_skips_no_trade_without_attempting_send(self) -> None:
        module = _load_module()
        module._read_last_signal_json = lambda: {"no_trade": True}
        module._build_signal_json_v1 = lambda **kwargs: {"unexpected": True}
        module._resolve_channel_id = lambda: -1001234567890
        send_calls = []
        module.send_signal_to_aia = lambda body: send_calls.append(body) or True

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = module.main()

        self.assertEqual(rc, 0)
        self.assertEqual(send_calls, [])
        self.assertIn("send_signal_to_aia(full): SKIP (NO_TRADE)", buf.getvalue())

    def test_main_reports_payload_build_failure(self) -> None:
        module = _load_module()
        module._read_last_signal_json = lambda: {"symbol": "BNB/USDT", "no_trade": False}
        module._build_signal_json_v1 = lambda **kwargs: None
        module._resolve_channel_id = lambda: -1001234567890
        send_calls = []
        module.send_signal_to_aia = lambda body: send_calls.append(body) or True

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = module.main()

        self.assertEqual(rc, 0)
        self.assertEqual(send_calls, [])

    def test_main_uses_explicit_last_json_instead_of_stale_default_path(self) -> None:
        module = _load_module()

        with TemporaryDirectory() as td:
            stale_last_json = Path(td) / "stale-last.json"
            fresh_last_json = Path(td) / "fresh-last.json"
            stale_last_json.write_text(
                json.dumps(
                    {
                        "symbol": "OLD/USDT",
                        "direction": "short",
                        "entry_range": [1.0, 2.0],
                        "no_trade": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            fresh_last_json.write_text(
                json.dumps(
                    {
                        "symbol": "NEW/USDT",
                        "direction": "long",
                        "entry_range": [10.0, 12.0],
                        "entry_price_neutral": 11.0,
                        "sl": 9.5,
                        "tp1": 13.0,
                        "tp2": 14.0,
                        "mode": "neutral",
                        "entry_mode": "pullback",
                        "sl_by_mode": {"neutral": 9.5},
                        "tp_by_mode": {"neutral": {"tvh1": 13.0, "tvh2": 14.0}},
                        "no_trade": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            module.LAST_JSON_PATH = stale_last_json
            module._resolve_channel_id = lambda payload=None: -1001234567890
            module._build_signal_json_v1 = lambda **kwargs: {
                "signal_id": "sig-fresh-1",
                "symbol": kwargs["last_payload"]["symbol"],
                "direction": kwargs["last_payload"]["direction"],
                "entry_zone": kwargs["last_payload"]["entry_range"],
                "entry_price": kwargs["last_payload"]["entry_price_neutral"],
                "sl": kwargs["last_payload"]["sl"],
                "tp": {
                    "tp1": kwargs["last_payload"]["tp1"],
                    "tp2": kwargs["last_payload"]["tp2"],
                },
                "published_at": "2026-04-05T17:35:00Z",
                "channel_id": -1001234567890,
            }
            send_calls = []
            module.send_signal_to_aia = lambda body: send_calls.append(body) or True

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "send_last_signal_to_aia.py",
                    "logs/signal_20260412_120000.log",
                    "--last-json",
                    str(fresh_last_json),
                ]
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = module.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(rc, 0)
            self.assertEqual(len(send_calls), 1)
            self.assertEqual(send_calls[0]["symbol"], "NEW/USDT")
            self.assertEqual(send_calls[0]["direction"], "long")
            self.assertEqual(send_calls[0]["entry_zone"], [10.0, 12.0])

            persisted_fresh = json.loads(fresh_last_json.read_text(encoding="utf-8"))
            persisted_stale = json.loads(stale_last_json.read_text(encoding="utf-8"))
            self.assertEqual(persisted_fresh["symbol"], "NEW/USDT")
            self.assertEqual(persisted_fresh["signal_json_v1"]["symbol"], "NEW/USDT")
            self.assertEqual(persisted_stale["symbol"], "OLD/USDT")
            self.assertIn("send_signal_to_aia(full): OK", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
