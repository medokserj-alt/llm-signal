import unittest


from get_signal_json import validate_active_mode_setup


class TestModeFallback(unittest.TestCase):
    def test_entry_range_matches_mode_when_enabled(self) -> None:
        d = {
            "mode": "aggressive",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 1, "max": 2}},
                "neutral": {"enabled": True, "range": {"min": 10, "max": 20}},
                "conservative": {"enabled": True, "range": {"min": 100, "max": 200}},
            },
            # Simulate previous behavior where entry_range could be neutral even with mode=aggressive.
            "entry_range": {"min": 10, "max": 20},
            "entry_price_aggressive": 1.5,
            "sl_by_mode": {"aggressive": 1.0},
            "tp_by_mode": {"aggressive": {"tvh1": 2.0, "tvh2": 2.5, "tvh3": 3.0}},
            "rr_by_mode": {"aggressive": 1.2},
            "exit_plan_by_mode": {"aggressive": "plan"},
        }
        out = validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("mode"), "aggressive")
        self.assertEqual(out.get("entry_range"), {"min": 1, "max": 2})

    def test_invalid_mode_normalizes_to_neutral_and_syncs_entry_range(self) -> None:
        d = {
            "mode": "banana",
            "entries": {
                "aggressive": {"enabled": True, "range": {"min": 1, "max": 2}},
                "neutral": {"enabled": True, "range": {"min": 10, "max": 20}},
                "conservative": {"enabled": True, "range": {"min": 100, "max": 200}},
            },
            "entry_range": {"min": 1, "max": 2},
            "entry_price_neutral": 15.0,
            "sl_by_mode": {"neutral": 14.0},
            "tp_by_mode": {"neutral": {"tvh1": 16.0, "tvh2": 17.0}},
            "rr_by_mode": {"neutral": 1.5},
            "exit_plan_by_mode": {"neutral": "plan"},
        }
        out = validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("mode"), "neutral")
        self.assertEqual(out.get("entry_range"), {"min": 10, "max": 20})

    def test_fallback_from_disabled_aggressive_to_neutral(self) -> None:
        d = {
            "mode": "aggressive",
            "entries": {
                "aggressive": {"enabled": False, "disabled_by": ["ema_guard_between"], "range": {"min": 1, "max": 2}},
                "neutral": {"enabled": True, "range": {"min": 10, "max": 20}},
                "conservative": {"enabled": True, "range": {"min": 100, "max": 200}},
            },
            "entry_range": {"min": 1, "max": 2},
            "entry_price_neutral": 15.0,
            "sl_by_mode": {"neutral": 14.0},
            "tp_by_mode": {"neutral": {"tvh1": 16.0, "tvh2": 17.0}},
            "rr_by_mode": {"neutral": 1.5},
            "exit_plan_by_mode": {"neutral": "plan"},
        }
        out = validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertEqual(out.get("mode"), "neutral")
        self.assertEqual(out.get("entry_range"), {"min": 10, "max": 20})
        self.assertIn("mode_fallback: aggressive->neutral", out.get("warnings") or [])
        self.assertIn("mode_disabled_by: aggressive: ema_guard_between", out.get("warnings") or [])

    def test_no_fallback_keeps_no_trade_and_includes_disabled_by(self) -> None:
        d = {
            "mode": "aggressive",
            "entries": {
                "aggressive": {"enabled": False, "disabled_by": ["ema_guard_between"]},
                "neutral": {"enabled": False},
                "conservative": {"enabled": False},
            },
        }
        out = validate_active_mode_setup(d)
        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("mode_disabled", out.get("no_trade_reasons") or [])
        self.assertIn("disabled_by=ema_guard_between", out.get("no_trade_hint") or "")


if __name__ == "__main__":
    unittest.main()
