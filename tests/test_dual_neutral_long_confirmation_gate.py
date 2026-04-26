import unittest

import get_signal_json


def _base_payload(mode: str) -> dict:
    return {
        "no_trade": False,
        "mode": mode,
        "symbol": "BTC/USDT",
        "price": 100.0,
        "side": "long",
        "entry_mode": "limit",
        "warnings": [],
        "day_mid_context": {"day_bias": "neutral", "mid_bias": "neutral", "notes": None},
        "entries": {
            "aggressive": {"enabled": True, "range": {"min": 99.4, "max": 99.7}},
            "neutral": {"enabled": True, "range": {"min": 98.5, "max": 99.0}},
            "conservative": {"enabled": True, "range": {"min": 97.5, "max": 98.0}},
        },
        "entry_range": {"min": 98.5, "max": 99.0},
        "entry_price_aggressive": 99.55,
        "entry_price_neutral": 98.75,
        "sl_by_mode": {"aggressive": 98.0, "neutral": 97.0},
        "tp_by_mode": {
            "aggressive": {"tvh1": 101.0, "tvh2": 102.0},
            "neutral": {"tvh1": 101.5, "tvh2": 103.0},
        },
        "rr_by_mode": {"aggressive": 1.2, "neutral": 1.4},
        "exit_plan_by_mode": {"aggressive": "plan", "neutral": "plan"},
    }


class TestDualNeutralLongConfirmationGate(unittest.TestCase):
    def test_neutral_dual_neutral_long_waits_when_structure_is_not_strong(self) -> None:
        d = _base_payload("neutral")
        d["price_vs_ema20_m15"] = "above"
        d["price_vs_ema20_h1"] = "below"
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "wait_confirm")
        self.assertIn("dual_neutral_long_requires_confirmation", out.get("warnings") or [])

    def test_aggressive_dual_neutral_long_waits_but_is_not_globally_blocked(self) -> None:
        d = _base_payload("aggressive")
        d["entry_mode"] = "now"
        d["price_vs_ema20_m15"] = "above"
        d["price_vs_ema20_h1"] = "below"
        d["ema_fan_m15_state"] = "mixed"
        d["ema_fan_h1_state"] = "mixed"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "wait_confirm")
        self.assertIn("dual_neutral_long_requires_confirmation", out.get("warnings") or [])

    def test_dual_neutral_long_keeps_strong_bull_structure_unchanged(self) -> None:
        d = _base_payload("neutral")
        d["price_vs_ema20_m15"] = "above"
        d["price_vs_ema20_h1"] = "above"
        d["ema_fan_m15_state"] = "bull"
        d["ema_fan_h1_state"] = "bull"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "limit")
        self.assertNotIn("dual_neutral_long_requires_confirmation", out.get("warnings") or [])


if __name__ == "__main__":
    unittest.main()
