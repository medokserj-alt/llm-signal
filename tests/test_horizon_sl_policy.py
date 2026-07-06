import unittest

import get_signal_json


class TestHorizonSlPolicy(unittest.TestCase):
    def test_neutral_long_widens_tight_sl_to_mode_floor_and_recalculates_rr(self) -> None:
        payload = {
            "symbol": "BTC/USDT",
            "mode": "neutral",
            "direction": "long",
            "side": "long",
            "entry_price_neutral": 62954.09,
            "sl_by_mode": {"neutral": 62550.61},
            "tp_by_mode": {"neutral": {"tvh1": 63600.0, "tvh2": 64200.0}},
            "entries": {"neutral": {"enabled": True, "range": {"min": 62954.09, "max": 62954.09}}},
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(payload)
        get_signal_json._sync_top_level_trade_levels_for_mode(out)  # type: ignore[attr-defined]

        self.assertAlmostEqual(out["sl_by_mode"]["neutral"], 62324.55, places=2)
        self.assertAlmostEqual(out["sl"], 62324.55, places=2)
        self.assertTrue(out["sl_policy_applied"])
        self.assertEqual(out["sl_policy_mode"], "neutral")
        self.assertAlmostEqual(out["min_sl_distance_pct"], 0.01, places=6)
        self.assertAlmostEqual(out["sl_distance_pct"], 0.01, places=6)
        self.assertLess(out["rr_by_mode"]["neutral"], 2.0)

    def test_aggressive_keeps_valid_existing_sl_when_above_aggressive_floor(self) -> None:
        payload = {
            "symbol": "ETH/USDT",
            "mode": "aggressive",
            "direction": "long",
            "side": "long",
            "entry_price_aggressive": 100.0,
            "sl_by_mode": {"aggressive": 99.2},
            "tp_by_mode": {"aggressive": {"tvh1": 101.0, "tvh2": 102.0, "tvh3": 103.0}},
            "entries": {"aggressive": {"enabled": True, "range": {"min": 100.0, "max": 100.0}}},
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(payload)

        self.assertAlmostEqual(out["sl_by_mode"]["aggressive"], 99.2, places=6)
        self.assertFalse(out["sl_policy_by_mode"]["aggressive"]["sl_policy_applied"])

    def test_conservative_short_uses_wider_floor_symmetrically(self) -> None:
        payload = {
            "symbol": "BNB/USDT",
            "mode": "conservative",
            "direction": "short",
            "side": "short",
            "entry_price_conservative": 500.0,
            "sl_by_mode": {"conservative": 505.0},
            "tp_by_mode": {"conservative": {"tvh1": 490.0, "tvh2_or_trail": 480.0}},
            "entries": {"conservative": {"enabled": True, "range": {"min": 500.0, "max": 500.0}}},
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(payload)

        self.assertAlmostEqual(out["sl_by_mode"]["conservative"], 507.5, places=6)
        self.assertTrue(out["sl_policy_by_mode"]["conservative"]["sl_policy_applied"])
        self.assertAlmostEqual(out["sl_policy_by_mode"]["conservative"]["min_sl_distance_pct"], 0.015, places=6)


if __name__ == "__main__":
    unittest.main()
