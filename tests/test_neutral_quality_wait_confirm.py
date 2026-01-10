import unittest

import get_signal_json


class TestNeutralQualityWaitConfirm(unittest.TestCase):
    def test_neutral_low_rr_forces_wait_confirm_not_no_trade(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "price": 100.0,
            "entry_mode": "now",
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 99.0},
            "tp_by_mode": {"neutral": {"tvh1": 100.8, "tvh2": 101.0}},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "wait_confirm")
        self.assertIn("neutral_wait_confirm_due_to_rr", out.get("warnings") or [])

    def test_hard_gate_still_blocks_even_if_rr_is_low(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "side": "long",
            "price": 100.0,
            "entry_mode": "now",
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_price_neutral": 100.0,
            "entry_price_aggressive": 99.5,
            "sl_by_mode": {"neutral": 99.0, "aggressive": 98.0},
            "tp_by_mode": {
                "neutral": {"tvh1": 100.8, "tvh2": 101.0},
                "aggressive": {"tvh1": 101.0, "tvh2": 102.0},
            },
            "exit_plan_by_mode": {"neutral": "plan", "aggressive": "plan"},
            "warnings": ["dir_guard_forced_short_by_ema"],
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(d)
        out = get_signal_json.validate_active_mode_setup(out)
        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("neutral_direction_conflict_with_ema", out.get("no_trade_reasons") or [])


if __name__ == "__main__":
    unittest.main()

