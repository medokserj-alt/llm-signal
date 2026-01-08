import unittest

import get_signal_json


def _base_conservative_long_payload() -> dict:
    return {
        "no_trade": False,
        "mode": "conservative",
        "symbol": "BTC/USDT",
        "price": 100.0,
        "side": "long",
        "warnings": [],
        "day_mid_context": {"day_bias": "neutral", "mid_bias": "long", "notes": None},
        "entries": {
            "neutral": {"enabled": True, "range": {"min": 98.5, "max": 99.0}},
            "aggressive": {"enabled": True, "range": {"min": 99.4, "max": 99.7}},
            "conservative": {"enabled": True, "range": {"min": 97.5, "max": 98.0}},
        },
        "entry_range": {"min": 97.5, "max": 98.0},
        "entry_price_neutral": 98.75,
        "entry_price_aggressive": 99.55,
        "entry_price_conservative": 97.75,
        "sl_by_mode": {"conservative": 96.5},
        "tp_by_mode": {"conservative": {"tvh1": 101.0, "tvh2_or_trail": "trail"}},
        "rr_by_mode": {"conservative": 2.2},
        "exit_plan_by_mode": {"conservative": "plan"},
        # Local context defaults (stable by default).
        "ema_fan_m15_state": "bull",
        "ema_fan_h1_state": "bull",
        "price_vs_ema20_m15": "above",
        "price_vs_ema20_h1": "above",
    }


class TestConservativeModeGates(unittest.TestCase):
    def test_conservative_blocked_when_mid_bias_missing_or_neutral(self) -> None:
        d = _base_conservative_long_payload()
        d["day_mid_context"] = {"day_bias": "neutral", "mid_bias": "neutral", "notes": None}

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("conservative_requires_mid_bias", out.get("no_trade_reasons") or [])

    def test_conservative_blocked_when_day_bias_opposes_mid_bias(self) -> None:
        d = _base_conservative_long_payload()
        d["day_mid_context"] = {"day_bias": "short", "mid_bias": "long", "notes": None}

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("conservative_day_mid_conflict", out.get("no_trade_reasons") or [])

    def test_conservative_blocked_on_flush_or_impulse_no_exhale(self) -> None:
        d = _base_conservative_long_payload()
        d["warnings"] = ["impulse_no_exhale"]
        d["ema_fan_m15_state"] = "bear"
        d["price_vs_ema20_m15"] = "below"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("conservative_local_not_stable", out.get("no_trade_reasons") or [])

    def test_conservative_allowed_when_mid_day_aligned_and_local_stable(self) -> None:
        d = _base_conservative_long_payload()
        d["day_mid_context"] = {"day_bias": "long", "mid_bias": "long", "notes": None}
        d["warnings"] = []
        d["ema_fan_m15_state"] = "bull"
        d["ema_fan_h1_state"] = "bull"
        d["price_vs_ema20_m15"] = "above"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("intended_horizon_hours"), {"min": 24, "max": 72})

    def test_conservative_no_trade_still_includes_horizon(self) -> None:
        d = _base_conservative_long_payload()
        d["no_trade"] = True
        d["intended_horizon_hours"] = None

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertEqual(out.get("intended_horizon_hours"), {"min": 24, "max": 72})


if __name__ == "__main__":
    unittest.main()
