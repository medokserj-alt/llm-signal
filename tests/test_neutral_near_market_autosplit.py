import unittest

import get_signal_json


class TestNeutralNearMarketAutoSplit(unittest.TestCase):
    def test_neutral_near_market_is_moved_to_calmer_offset(self) -> None:
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
            "sl_by_mode": {"neutral": 0.4900},
            "tp_by_mode": {"neutral": {"tvh1": 0.5100, "tvh2": 0.5200}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertEqual(out.get("mode"), "neutral")

        self.assertFalse(bool(out.get("no_trade")))
        self.assertAlmostEqual(float(out.get("neutral_offset_pct")), 1.0, places=6)

        px = float(out["price"])
        n_entry = float(out["entry_price_neutral"])
        self.assertLessEqual(n_entry, px - px * 0.010)


if __name__ == "__main__":
    unittest.main()
