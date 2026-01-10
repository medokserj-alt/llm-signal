import unittest

import get_signal_json


class TestNeutralEntryRangeEdgeSelection(unittest.TestCase):
    def test_neutral_uses_conservative_edge_of_range_long(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 99.0, "max": 101.0}},
            },
            "entry_range": {"min": 99.0, "max": 101.0},
            # Intentionally wrong: should be overwritten to range.min for neutral-long.
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertAlmostEqual(float(out["entry_price_neutral"]), 99.0, places=8)

    def test_neutral_uses_conservative_edge_of_range_short(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "short",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 99.0, "max": 101.0}},
            },
            "entry_range": {"min": 99.0, "max": 101.0},
            # Intentionally wrong: should be overwritten to range.max for neutral-short.
            "entry_price_neutral": 100.0,
            "sl_by_mode": {"neutral": 103.0},
            "tp_by_mode": {"neutral": {"tvh1": 98.0, "tvh2": 96.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertAlmostEqual(float(out["entry_price_neutral"]), 101.0, places=8)

    def test_neutral_wait_confirm_is_farther_than_aggressive_option_when_safe(self) -> None:
        px = 0.5000
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "XRP/USDT",
            "price": px,
            "side": "long",
            "entry_mode": "wait_confirm",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 0.4950, "max": 0.5020}},
                "aggressive": {"enabled": True, "range": {"min": 0.4998, "max": 0.5002}},
            },
            "entry_range": {"min": 0.4950, "max": 0.5020},
            "entry_price_neutral": 0.5000,
            "entry_price_aggressive": 0.4999,
            "aggressive_option": {"entry_price": 0.4999, "note": "aggressive"},
            "sl_by_mode": {"neutral": 0.4900},
            "tp_by_mode": {"neutral": {"tvh1": 0.5100, "tvh2": 0.5200}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        a_entry = float(out["aggressive_option"]["entry_price"])
        n_entry = float(out["entry_price_neutral"])
        self.assertGreater(abs(px - n_entry), abs(px - a_entry))
        self.assertLess(n_entry, a_entry)


if __name__ == "__main__":
    unittest.main()

