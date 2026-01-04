import unittest

import get_signal_json


class TestNeutralNearMarketAutoSplit(unittest.TestCase):
    def test_neutral_near_market_creates_aggressive_option_and_buffers_neutral(self) -> None:
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
        buf = get_signal_json.NEUTRAL_BUFFER_TICKS * tick
        original_entry = float(d["entry_price_neutral"])

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertEqual(out.get("mode"), "neutral")

        self.assertIsInstance(out.get("aggressive_option"), dict)
        a_entry = float(out["aggressive_option"]["entry_price"])
        n_entry = float(out["entry_price_neutral"])

        self.assertAlmostEqual(a_entry, original_entry, places=7)
        self.assertGreaterEqual(a_entry - n_entry + 1e-12, buf)

        px = float(out["price"])
        dist_ticks = abs(n_entry - px) / tick
        self.assertGreater(dist_ticks, get_signal_json.NEUTRAL_NEAR_TICKS)
        self.assertIs(out.get("neutral_autosplit"), True)


if __name__ == "__main__":
    unittest.main()
