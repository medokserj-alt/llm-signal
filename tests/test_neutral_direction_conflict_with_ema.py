import unittest

import get_signal_json


class TestNeutralDirectionConflictWithEma(unittest.TestCase):
    def test_neutral_direction_conflict_becomes_no_trade_and_keeps_aggressive_option(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "entry_mode": "limit",
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_price_neutral": 99.0,
            "entry_price_aggressive": 99.5,
            "sl_by_mode": {"neutral": 95.0, "aggressive": 96.0},
            "tp_by_mode": {"neutral": {"tvh1": 110.0, "tvh2": 120.0}, "aggressive": {"tvh1": 103.0, "tvh2": 106.0}},
            "rr_by_mode": {"neutral": 1.0, "aggressive": 1.0},
            "exit_plan_by_mode": {"neutral": "plan", "aggressive": "plan"},
            "warnings": ["dir_guard_forced_short_by_ema"],
        }

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertEqual(out.get("no_trade_reason"), "neutral_direction_conflict_with_ema")
        self.assertIn("neutral_direction_conflict_with_ema", out.get("no_trade_reasons") or [])

        self.assertIsNone(out.get("entry_price_neutral"))
        self.assertIsNone((out.get("sl_by_mode") or {}).get("neutral"))
        self.assertIsNone((out.get("tp_by_mode") or {}).get("neutral"))

        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertAlmostEqual(float(aggressive_option.get("entry_price")), 99.5, places=6)


if __name__ == "__main__":
    unittest.main()

