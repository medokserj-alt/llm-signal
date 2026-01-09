import unittest

import get_signal_json


class TestAggressiveTradeOriented(unittest.TestCase):
    def test_aggressive_low_rr_does_not_force_no_trade(self) -> None:
        d = {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "entry_mode": "now",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 99.9, "max": 100.1}},
                "neutral": {"enabled": True, "range": {"min": 99.0, "max": 99.5}},
                "conservative": {"enabled": True, "range": {"min": 98.0, "max": 98.5}},
            },
            "entry_price_aggressive": 100.0,
            # Make RR too low for aggressive (risk wide vs modest TP fallback).
            "sl_by_mode": {"aggressive": 97.0},
            "tp_by_mode": {"aggressive": {}},
            "rr_by_mode": {"aggressive": 0.2},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "warnings": [],
            "ema_m15": {},
            "ema_h1": {},
            "ema_fan_m15_state": "bull",
            "ema_fan_h1_state": "bull",
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("entry_mode"), "wait_confirm")
        self.assertIn("low_rr_aggressive", out.get("warnings") or [])

    def test_aggressive_time_window_forces_wait_confirm_without_extreme(self) -> None:
        d = {
            "mode": "aggressive",
            "time_msk": "01.01.2025, 00:30",
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
            "entry_mode": "now",
        }
        get_signal_json.apply_time_window_policy_variant_b(d)
        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertIn("time_window_caution_aggressive", d.get("warnings") or [])

    def test_aggressive_time_window_extreme_blocks_on_risk_off(self) -> None:
        d = {
            "mode": "aggressive",
            "time_msk": "01.01.2025, 00:30",
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
            "entry_mode": "now",
            "risk_off": True,
        }
        get_signal_json.apply_time_window_policy_variant_b(d)
        self.assertTrue(bool(d.get("no_trade")))
        self.assertIn("time_window_extreme_block", d.get("no_trade_reasons") or [])
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertIn("time_window_caution_aggressive", d.get("warnings") or [])

    def test_aggressive_countertrend_without_evidence_waits_confirmation(self) -> None:
        d = {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "short",
            "entry_mode": "now",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 100.1, "max": 100.4}},
                "neutral": {"enabled": True, "range": {"min": 100.5, "max": 101.0}},
                "conservative": {"enabled": True, "range": {"min": 101.5, "max": 102.0}},
            },
            "entry_range": {"min": 100.1, "max": 100.4},
            "entry_price_aggressive": 100.25,
            "sl_by_mode": {"aggressive": 101.0},
            "tp_by_mode": {"aggressive": {"tvh1": 99.7, "tvh2": 99.0}},
            "rr_by_mode": {"aggressive": 1.1},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "price_vs_ema20_h1": "above",
            "ema_fan_h1_state": "bull",
            "ema_fan_m15_state": "bull",
            "ema_guard_state": "above_both",
            "warnings": [],
            "confidence": "High",
        }

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertNotEqual(out.get("entry_mode"), "now")
        self.assertIn("countertrend_aggressive_needs_evidence", out.get("warnings") or [])

    def test_aggressive_dir_guard_forced_short_by_ema_blocks_enter_now(self) -> None:
        d = {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "entry_mode": "now",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 99.9, "max": 100.1}},
                "neutral": {"enabled": True, "range": {"min": 99.0, "max": 99.5}},
                "conservative": {"enabled": True, "range": {"min": 98.0, "max": 98.5}},
            },
            "entry_range": {"min": 99.9, "max": 100.1},
            "price_vs_ema20_h1": "below",
            "ema_fan_h1_state": "bear",
            "ema_fan_m15_state": "bear",
            "warnings": ["dir_guard_forced_short_by_ema"],
        }

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertEqual(out.get("entry_mode"), "wait_confirm")
        self.assertIn("dir_guard_forced_short_by_ema", out.get("warnings") or [])
        self.assertIn("aggressive_direction_conflict_with_ema_wait_confirm", out.get("warnings") or [])

    def test_aggressive_dir_guard_with_reversal_evidence_may_enter_now(self) -> None:
        d = {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "entry_mode": "now",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 99.9, "max": 100.1}},
                "neutral": {"enabled": True, "range": {"min": 99.0, "max": 99.5}},
                "conservative": {"enabled": True, "range": {"min": 98.0, "max": 98.5}},
            },
            "entry_range": {"min": 99.9, "max": 100.1},
            "price_vs_ema20_h1": "below",
            "ema_fan_h1_state": "bear",
            # Existing reversal evidence heuristic for long: bull on M15 is enough.
            "ema_fan_m15_state": "bull",
            "warnings": ["dir_guard_forced_short_by_ema"],
        }

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertEqual(out.get("entry_mode"), "wait_confirm")
        self.assertIn("dir_guard_forced_short_by_ema", out.get("warnings") or [])
        self.assertIn("aggressive_direction_conflict_with_ema_wait_confirm", out.get("warnings") or [])

    def test_day_mid_is_bias_intraday_can_override_with_note(self) -> None:
        d = {
            "no_trade": False,
            "mode": "aggressive",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "short",
            "entry_mode": "limit",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 100.1, "max": 100.4}},
                "neutral": {"enabled": True, "range": {"min": 100.5, "max": 101.0}},
                "conservative": {"enabled": True, "range": {"min": 101.5, "max": 102.0}},
            },
            "entry_range": {"min": 100.1, "max": 100.4},
            "entry_price_aggressive": 100.25,
            "sl_by_mode": {"aggressive": 101.0},
            "tp_by_mode": {"aggressive": {"tvh1": 99.7, "tvh2": 99.0}},
            "rr_by_mode": {"aggressive": 1.1},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "day_mid_context": {"day_bias": "long", "mid_bias": "long", "notes": None},
            "price_vs_ema20_h1": "below",
            "ema_fan_h1_state": "bear",
        }

        out = get_signal_json.validate_active_mode_setup(d)

        ctx = out.get("day_mid_context") or {}
        self.assertIsInstance(ctx, dict)
        self.assertIn("override_note", ctx)
        self.assertIn("Расхождение с DAY/MID", ctx.get("override_note") or "")


if __name__ == "__main__":
    unittest.main()
