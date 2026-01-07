import unittest

import get_signal_json


def _base_neutral_short_payload() -> dict:
    return {
        "no_trade": False,
        "mode": "neutral",
        "symbol": "BTC/USDT",
        "price": 100.0,
        "side": "short",
        "entries": {
            "neutral": {"enabled": True, "range": {"min": 100.5, "max": 101.0}},
            "aggressive": {"enabled": True, "range": {"min": 100.1, "max": 100.4}},
            "conservative": {"enabled": True, "range": {"min": 101.5, "max": 102.0}},
        },
        "entry_range": {"min": 100.5, "max": 101.0},
        "entry_price_neutral": 100.75,
        "entry_price_aggressive": 100.25,
        "sl_by_mode": {"neutral": 102.0},
        "tp_by_mode": {"neutral": {"tvh1": 99.0, "tvh2": 98.0}},
        "rr_by_mode": {"neutral": 1.2},
        "exit_plan_by_mode": {"neutral": "plan"},
        # Required context fields for the gate (set in tests per case).
        "price_vs_ema20_h1": "above",
        "ema_fan_h1_state": "bull",
        "ema_fan_m15_state": "bull",
        "ema_guard_state": "above_both",
    }


class TestNeutralCounterTrendGate(unittest.TestCase):
    def test_neutral_counter_trend_forbidden_when_h1_bull_no_reversal(self) -> None:
        d = _base_neutral_short_payload()
        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("counter_trend_neutral_forbidden", out.get("no_trade_reasons") or [])

        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertIn("Контртрендовая идея", aggressive_option.get("note") or "")
        self.assertIn("aggressive", aggressive_option.get("note") or "")
        self.assertIsNotNone(aggressive_option.get("entry_price"))

    def test_neutral_counter_trend_allowed_when_h1_mixed(self) -> None:
        d = _base_neutral_short_payload()
        d["ema_fan_h1_state"] = "mixed"
        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertNotIn("counter_trend_neutral_forbidden", out.get("no_trade_reasons") or [])

    def test_neutral_counter_trend_allowed_when_m15_bear(self) -> None:
        d = _base_neutral_short_payload()
        d["ema_fan_m15_state"] = "bear"
        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("counter_trend_neutral_forbidden", out.get("no_trade_reasons") or [])

    def test_neutral_counter_trend_forbidden_for_long_in_strong_h1_downtrend(self) -> None:
        d = _base_neutral_short_payload()
        d["side"] = "long"
        d["price_vs_ema20_h1"] = "below"
        d["ema_fan_h1_state"] = "bear"
        d["entry_price_neutral"] = 99.25
        d["entry_price_aggressive"] = 99.75

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("counter_trend_neutral_forbidden", out.get("no_trade_reasons") or [])
        self.assertIsInstance(out.get("aggressive_option"), dict)


if __name__ == "__main__":
    unittest.main()
