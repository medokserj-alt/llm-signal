import unittest

import get_signal_json


class TestConservativeRrPolicy(unittest.TestCase):
    def test_conservative_trail_allows_min_rr_to_tp1(self) -> None:
        d = {
            "no_trade": False,
            "mode": "conservative",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "ema_m15": {},
            "ema_h1": {},
            "entries": {
                "aggressive": {"enabled": True},
                "neutral": {"enabled": True},
                "conservative": {"enabled": True},
            },
            "entry_price_conservative": 100.0,
            "sl_by_mode": {"conservative": 99.0},
            # RR to TP1 = 1.0, trail handles the rest.
            "tp_by_mode": {"conservative": {"tvh1": 101.0, "tvh2_or_trail": "trail"}},
            "exit_plan_by_mode": {"conservative": "plan"},
            "warnings": [],
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertIn("conservative_trail_rr_assumed", out.get("warnings") or [])

    def test_conservative_numeric_tp2_requires_higher_rr(self) -> None:
        d = {
            "no_trade": False,
            "mode": "conservative",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "ema_m15": {},
            "ema_h1": {},
            "entries": {
                "aggressive": {"enabled": True},
                "neutral": {"enabled": True},
                "conservative": {"enabled": True},
            },
            "entry_price_conservative": 100.0,
            "sl_by_mode": {"conservative": 99.0},
            # RR to TP2 = 1.0 (< 1.5) => blocked.
            "tp_by_mode": {"conservative": {"tvh1": 101.0, "tvh2_or_trail": 101.0}},
            "exit_plan_by_mode": {"conservative": "plan"},
            "warnings": [],
        }

        out = get_signal_json.validate_or_fallback_tvh_by_mode(d)
        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("недостаточный RR для входа", out.get("no_trade_reasons") or [])


if __name__ == "__main__":
    unittest.main()

