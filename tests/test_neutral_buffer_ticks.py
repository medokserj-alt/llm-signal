import unittest

import get_signal_json


class TestNeutralBufferTicks(unittest.TestCase):
    def test_xrp_precision_neutral_buffer_applies_when_aggressive_option_exists(self) -> None:
        # XRP-like precision should use 4 decimals => tick = 0.0001
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "XRP/USDT",
            "price": 0.5000,
            "side": "long",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 0.4980, "max": 0.5020}},
                "aggressive": {"enabled": True, "range": {"min": 0.4998, "max": 0.5002}},
                "conservative": {"enabled": True, "range": {"min": 0.4970, "max": 0.4985}},
            },
            "entry_range": {"min": 0.4980, "max": 0.5020},
            # Far enough from market (avoid strict-neutral near-market rejection),
            # but still must satisfy neutral buffer vs aggressive.
            "entry_price_neutral": 0.4988,
            "entry_price_aggressive": 0.5000,
            "aggressive_option": {"entry_price": 0.5000, "note": "aggressive"},
            "sl_by_mode": {"neutral": 0.4950},
            "tp_by_mode": {"neutral": {"tvh1": 0.5100, "tvh2": 0.5200}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertEqual(out.get("mode"), "neutral")

        tick = 10 ** (-get_signal_json._price_precision("XRP/USDT", value_hint=0.5))
        buf = get_signal_json.NEUTRAL_BUFFER_TICKS * tick
        a_entry = float(out["aggressive_option"]["entry_price"])
        n_entry = float(out["entry_price_neutral"])
        self.assertGreaterEqual(a_entry - n_entry + 1e-12, buf)


if __name__ == "__main__":
    unittest.main()
