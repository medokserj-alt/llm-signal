import unittest

import get_signal_json


def _base_neutral_long_payload() -> dict:
    return {
        "no_trade": False,
        "mode": "neutral",
        "symbol": "BTC/USDT",
        "price": 100.0,
        "side": "long",
        "entries": {
            "neutral": {"enabled": True, "range": {"min": 98.5, "max": 99.0}},
            "aggressive": {"enabled": True, "range": {"min": 99.4, "max": 99.7}},
            "conservative": {"enabled": True, "range": {"min": 97.5, "max": 98.0}},
        },
        "entry_range": {"min": 98.5, "max": 99.0},
        "entry_price_neutral": 98.75,
        "entry_price_aggressive": 99.55,
        "sl_by_mode": {"neutral": 97.0},
        "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
        "rr_by_mode": {"neutral": 1.8},
        "exit_plan_by_mode": {"neutral": "plan"},
        # Context fields used by other neutral gates; set to a safe state (no counter-trend forbid).
        "price_vs_ema20_h1": "above",
        "ema_fan_h1_state": "bull",
        "ema_guard_state": "above_both",
    }


class TestNeutralFlushReversalGate(unittest.TestCase):
    def test_neutral_blocked_on_flush(self) -> None:
        d = _base_neutral_long_payload()
        d["ema_fan_m15_state"] = "bear"
        d["price_vs_ema20_m15"] = "below"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("flush_reversal_neutral_forbidden", out.get("no_trade_reasons") or [])

    def test_aggressive_option_is_shown_for_flush_reversal(self) -> None:
        d = _base_neutral_long_payload()
        d["ema_fan_m15_state"] = "bear"
        d["price_vs_ema20_m15"] = "below"

        out = get_signal_json.validate_active_mode_setup(d)

        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertEqual(
            aggressive_option.get("note"),
            "Разворот после импульсного пролива — допустимо только в aggressive.",
        )
        self.assertIsNotNone(aggressive_option.get("entry_price"))

    def test_neutral_allowed_on_smooth_pullback(self) -> None:
        d = _base_neutral_long_payload()
        # Pullback is allowed in neutral; avoid flush structural trigger.
        d["ema_fan_m15_state"] = "bull"
        d["price_vs_ema20_m15"] = "below"

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertNotIn("flush_reversal_neutral_forbidden", out.get("no_trade_reasons") or [])


if __name__ == "__main__":
    unittest.main()

