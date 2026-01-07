import unittest

import get_signal_json


class TestTimeWindowPolicy(unittest.TestCase):
    def test_neutral_time_window_green_lane_allows_trade(self) -> None:
        d = {
            "mode": "neutral",
            "time_msk": "01.01.2025, 00:30",
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
            "side": "long",
            "price_vs_ema20_h1": "above",
            "ema_fan_h1_state": "bull",
            "ema_fan_m15_state": "bull",
        }
        get_signal_json.apply_time_window_policy_variant_b(d)
        self.assertFalse(bool(d.get("no_trade")))
        self.assertIn("time_window_low_liquidity", d.get("warnings") or [])
        self.assertIn("time_window_green_lane", d.get("warnings") or [])

    def test_neutral_time_window_blocks_by_default_and_keeps_aggressive_option(self) -> None:
        d = {
            "mode": "neutral",
            "time_msk": "01.01.2025, 00:30",
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
            "side": "long",
            # Not a clear continuation+stable state -> should be blocked.
            "price_vs_ema20_h1": "above",
            "ema_fan_h1_state": "bull",
            "ema_fan_m15_state": "mixed",
            "aggressive_option": {"entry_price": 100.0, "note": "ok"},
        }
        get_signal_json.apply_time_window_policy_variant_b(d)
        self.assertTrue(bool(d.get("no_trade")))
        self.assertIn("time_window", d.get("no_trade_reasons") or [])
        self.assertEqual((d.get("aggressive_option") or {}).get("entry_price"), 100.0)


if __name__ == "__main__":
    unittest.main()

