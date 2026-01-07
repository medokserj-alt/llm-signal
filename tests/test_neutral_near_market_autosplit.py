import unittest

import get_signal_json


class TestNeutralNearMarketAutoSplit(unittest.TestCase):
    def test_neutral_near_market_is_rejected_and_shows_aggressive_option_only(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "XRP/USDT",
            "price": 0.5000,
            "side": "long",
            "entries": {
                "neutral": {"enabled": True},
            },
            # Near-market neutral (1 tick away for XRP-like tick=0.0001).
            "entry_price_neutral": 0.4999,
            "sl_by_mode": {"neutral": 0.4950},
            "tp_by_mode": {"neutral": {"tvh1": 0.5100, "tvh2": 0.5200}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        tick = 10 ** (-get_signal_json._price_precision("XRP/USDT", value_hint=0.5))
        original_entry = float(d["entry_price_neutral"])

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertEqual(out.get("mode"), "neutral")

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("neutral_too_close_risky", out.get("no_trade_reasons") or [])

        self.assertIsInstance(out.get("aggressive_option"), dict)
        a_entry = float(out["aggressive_option"]["entry_price"])
        self.assertAlmostEqual(a_entry, original_entry, places=7)

        # Still near-market => neutral is rejected in strict-neutral.
        px = float(out["price"])
        dist_ticks = abs(original_entry - px) / tick
        self.assertLessEqual(dist_ticks, get_signal_json.NEUTRAL_NEAR_TICKS)


if __name__ == "__main__":
    unittest.main()
