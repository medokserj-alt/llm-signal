import unittest

import get_signal_json


class TestNeutralTooCloseNoTrade(unittest.TestCase):
    def test_neutral_too_close_is_shifted_by_volatility_offset(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "UNI/USDT",
            "price": 15.00,
            "side": "long",
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            # Even after volatility-aware offset (1% for alts), this will stay within 20 ticks (tick=0.01).
            "entry_price_neutral": 14.99,
            "entry_price_aggressive": 14.95,
            # Ensure the shifted target remains valid and above SL.
            "sl_by_mode": {"neutral": 14.00},
            "tp_by_mode": {"neutral": {"tvh1": 15.50, "tvh2": 16.00}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertIn("neutral_entry_shifted_by_volatility", out.get("warnings") or [])
        self.assertAlmostEqual(float(out.get("entry_price_neutral")), 14.85, places=6)


if __name__ == "__main__":
    unittest.main()
