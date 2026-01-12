import unittest

import get_signal_json


class TestAggressiveCountertrendEvidence(unittest.TestCase):
    def _base_aggressive(self) -> dict:
        return {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "short",
            "entry_mode": "market",
            "entries": {"aggressive": {"enabled": True, "range": {"min": 99.0, "max": 101.0}}},
            "entry_range": {"min": 99.0, "max": 101.0},
            "entry_price_aggressive": 100.0,
            "sl_by_mode": {"aggressive": 105.0},
            "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0, "tvh3": 85.0}},
            "rr_by_mode": {"aggressive": 2.0},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "warnings": [],
        }

    def test_aggressive_countertrend_without_evidence_forces_wait_confirm(self) -> None:
        d = self._base_aggressive()
        # Strong H1 uptrend => SHORT is countertrend
        d["price_vs_ema20_h1"] = "above"
        d["ema_fan_h1_state"] = "bull"

        # No reversal evidence
        d["ema20_m15"] = 100.0
        d["closes_m15_tail"] = [101.0, 101.0]  # not below EMA20 for short
        d["price_vs_ema20_m15"] = "above"
        # Avoid triggering flush/knife extreme structural fallback for SHORT (bull + above).
        d["ema_fan_m15_state"] = "mixed"
        d["phase_flip_m15"] = False
        d["impulse_proxy"] = False

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "wait_confirm")
        self.assertIn("aggressive_countertrend_no_evidence_wait_confirm", out.get("warnings") or [])

    def test_aggressive_countertrend_with_evidence_allows_trade(self) -> None:
        d = self._base_aggressive()
        # Strong H1 uptrend => SHORT is countertrend
        d["price_vs_ema20_h1"] = "above"
        d["ema_fan_h1_state"] = "bull"

        # Evidence: M15 fan flipped into trade direction (bear for short)
        d["ema_fan_m15_state"] = "bear"
        d["ema20_m15"] = 100.0
        d["closes_m15_tail"] = [99.0, 99.0]
        d["price_vs_ema20_m15"] = "below"
        d["phase_flip_m15"] = False
        d["impulse_proxy"] = False

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertIn("aggressive_countertrend_with_evidence", out.get("warnings") or [])


if __name__ == "__main__":
    unittest.main()
