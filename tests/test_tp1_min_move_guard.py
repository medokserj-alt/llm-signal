import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import get_signal_json
import postprocess_full_last


class TestTp1MinMoveGuard(unittest.TestCase):
    def test_neutral_long_too_close_tp1_is_restored_not_no_trade(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "price": 100.8,
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 99.0},
            "rr_by_mode": {"neutral": 0.8},
            "entries": {"neutral": {"enabled": True, "range": {"min": 99.8, "max": 100.2}}},
            "tp_by_mode": {"neutral": {"tvh1": 100.5, "tvh2": 100.8}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("tp_by_mode", {}).get("neutral", {}).get("tvh1"), 101.0)
        self.assertEqual(out.get("tp_by_mode", {}).get("neutral", {}).get("tvh2"), 102.0)
        self.assertEqual(out.get("tp1"), 101.0)
        self.assertEqual(out.get("tp2"), 102.0)
        self.assertEqual(out.get("rr"), 2.0)

    def test_neutral_short_too_close_tp1_is_restored_not_no_trade(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "short",
            "price": 99.7,
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 101.0},
            "rr_by_mode": {"neutral": 0.8},
            "entries": {"neutral": {"enabled": True, "range": {"min": 99.8, "max": 100.2}}},
            "tp_by_mode": {"neutral": {"tvh1": 99.5, "tvh2": 99.2}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("tp_by_mode", {}).get("neutral", {}).get("tvh1"), 99.0)
        self.assertEqual(out.get("tp_by_mode", {}).get("neutral", {}).get("tvh2"), 98.0)
        self.assertEqual(out.get("tp1"), 99.0)
        self.assertEqual(out.get("tp2"), 98.0)
        self.assertEqual(out.get("rr"), 2.0)

    def test_aggressive_restore_targets_and_syncs_top_level(self) -> None:
        d = {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_price_aggressive": 100.0,
            "sl_by_mode": {"aggressive": 99.0},
            "rr_by_mode": {"aggressive": 0.5},
            "entries": {"aggressive": {"enabled": True, "range": {"min": 99.9, "max": 100.1}}},
            "tp_by_mode": {"aggressive": {"tvh1": 100.5, "tvh2": 100.8, "tvh3": 101.2}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("tp_by_mode", {}).get("aggressive", {}).get("tvh1"), 101.0)
        self.assertEqual(out.get("tp_by_mode", {}).get("aggressive", {}).get("tvh2"), 102.0)
        self.assertEqual(out.get("tp_by_mode", {}).get("aggressive", {}).get("tvh3"), 103.0)
        self.assertEqual(out.get("entry"), 100.0)
        self.assertEqual(out.get("entry_price"), 100.0)
        self.assertEqual(out.get("entry_range"), {"min": 99.9, "max": 100.1})
        self.assertEqual(out.get("tp"), {"tp1": 101.0, "tp2": 102.0, "tp3": 103.0})
        self.assertEqual(out.get("rr"), 3.0)

    def test_conservative_restore_keeps_trail_contract(self) -> None:
        d = {
            "no_trade": False,
            "mode": "conservative",
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_price_conservative": 100.0,
            "sl_by_mode": {"conservative": 99.0},
            "rr_by_mode": {"conservative": 0.4},
            "entries": {"conservative": {"enabled": True, "range": {"min": 99.0, "max": 100.0}}},
            "tp_by_mode": {"conservative": {"tvh1": 100.4, "tvh2_or_trail": "trail"}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("tp_by_mode", {}).get("conservative", {}).get("tvh1"), 101.0)
        self.assertEqual(out.get("tp_by_mode", {}).get("conservative", {}).get("tvh2_or_trail"), "trail")
        self.assertEqual(out.get("tp1"), 101.0)
        self.assertEqual(out.get("rr"), 1.0)

    def test_existing_no_trade_reason_is_not_overwritten(self) -> None:
        d = {
            "no_trade": True,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_price_neutral": 100.0,
            "tp_by_mode": {"neutral": {"tvh1": 100.5, "tvh2": 101.2}},
            "no_trade_reasons": ["time_window"],
            "no_trade_hint": "time_window",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertEqual(out.get("no_trade_reasons"), ["time_window"])
        self.assertEqual(out.get("no_trade_hint"), "time_window")

    def test_current_price_near_tp1_does_not_bypass_entry_anchor(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "price": 101.4,
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 99.0},
            "rr_by_mode": {"neutral": 0.9},
            "tp_by_mode": {"neutral": {"tvh1": 100.5, "tvh2": 100.9}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("tp1"), 101.0)
        self.assertIn("mode_target_ladder_restored:neutral", out.get("warnings") or [])

    def test_no_trade_only_after_restore_is_impossible(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "tp_by_mode": {"neutral": {"tvh1": 100.5, "tvh2": 100.8}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        out = get_signal_json.apply_tp1_min_move_guard(d)
        self.assertFalse(bool(out.get("no_trade")))

        out = get_signal_json.validate_active_mode_setup(out)
        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("invalid_mode_setup", out.get("no_trade_reasons") or [])
        self.assertNotIn("tp1_below_min_move", out.get("no_trade_reasons") or [])

    def test_tp1_min_move_guard_uses_trader_facing_hint_when_blocked(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 99.0},
            "tp_by_mode": {"neutral": {"tvh1": 100.4}},
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
        }

        with patch.object(get_signal_json, "restore_mode_target_ladder", side_effect=lambda payload: payload):
            out = get_signal_json.apply_tp1_min_move_guard(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("слишком близко к ближайшим целям", out.get("no_trade_hint") or "")
        self.assertIn("нужен либо откат", (out.get("no_trade_hint") or "").lower())
        self.assertNotIn("snapshot", out.get("no_trade_hint") or "")

    def test_postprocess_full_last_restores_targets_on_final_payload(self) -> None:
        old_base = postprocess_full_last.BASE
        try:
            with tempfile.TemporaryDirectory() as td:
                base = Path(td)
                postprocess_full_last.BASE = base
                (base / "logs").mkdir(parents=True, exist_ok=True)
                payload = {
                    "symbol": "BTC/USDT",
                    "price": 101.0,
                    "mode": "neutral",
                    "side": "long",
                    "no_trade": False,
                    "no_trade_reasons": [],
                    "no_trade_hint": "",
                    "warnings": [],
                    "entry_price_neutral": 100.0,
                    "sl_by_mode": {"neutral": 99.0},
                    "tp_by_mode": {"neutral": {"tvh1": 100.5, "tvh2": 100.8}},
                    "rr_by_mode": {"neutral": 1.2},
                    "exit_plan_by_mode": {"neutral": "plan"},
                    "entries": {"neutral": {"enabled": True, "range": {"min": 99.8, "max": 100.2}}},
                    "why_asset": "test",
                }
                (base / "logs" / "last.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                with (
                    patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=lambda _d: None),
                    patch.object(postprocess_full_last, "apply_ema_blocks_and_derivatives", side_effect=lambda *_a, **_k: None),
                    patch.object(postprocess_full_last, "_apply_ema_guard_text_consistent", side_effect=lambda _d: None),
                    patch.object(postprocess_full_last, "pp_process", side_effect=lambda d, *_a: d),
                    patch.object(postprocess_full_last, "merge_day_mid_report_context", side_effect=lambda *_a, **_k: None),
                    patch.object(postprocess_full_last, "merge_event_calendar_context", side_effect=lambda *_a, **_k: None),
                    patch.object(postprocess_full_last, "apply_ema_exhale_filter", side_effect=lambda _d: None),
                    patch.object(postprocess_full_last, "_ensure_by_mode_levels", side_effect=lambda d: d),
                    patch.object(postprocess_full_last, "validate_active_mode_setup", side_effect=lambda d: d),
                    patch.object(postprocess_full_last, "sync_impulse_proxy", side_effect=lambda _d: None),
                    patch.object(postprocess_full_last, "apply_upcoming_event_risk", side_effect=lambda _d: None),
                    patch.object(postprocess_full_last, "read_latest_report_payload", return_value={}),
                ):
                    postprocess_full_last.main()

                out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
                self.assertFalse(bool(out.get("no_trade")))
                self.assertEqual(out.get("tp1"), 101.0)
                self.assertEqual(out.get("tp2"), 102.0)
                self.assertEqual(out.get("rr"), 2.0)
        finally:
            postprocess_full_last.BASE = old_base


if __name__ == "__main__":
    unittest.main()
