import unittest

import get_signal_json


class TestEntryPriceSelectionFromLlmRanges(unittest.TestCase):
    def test_neutral_long_range_selects_min(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "long",
            "entries": {"neutral": {"range": {"min": 99.0, "max": 101.0}}},
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_neutral"]), 99.0, places=8)

    def test_neutral_short_range_selects_max(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "short",
            "entries": {"neutral": {"range": {"min": 99.0, "max": 101.0}}},
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_neutral"]), 101.0, places=8)

    def test_aggressive_midpoint_when_not_wait_confirm(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_mode": "limit",
            "entries": {"aggressive": {"range": {"min": 99.0, "max": 101.0}}},
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_aggressive"]), 100.0, places=8)

    def test_range_source_priority_entries_bucket_over_entry_range(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "long",
            # Top-level entry_range exists but must NOT be used when entries[neutral].range exists.
            "entry_range": {"min": 80.0, "max": 81.0},
            "entries": {"neutral": {"range": {"min": 90.0, "max": 91.0}}},
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_neutral"]), 90.0, places=8)

    def test_range_fallback_entry_range_when_mode_bucket_missing(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "short",
            # No entries[conservative].range -> should use top-level entry_range.
            "entries": {"neutral": {"range": {"min": 99.0, "max": 101.0}}},
            "entry_range": {"min": 10.0, "max": 20.0},
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_conservative"]), 20.0, places=8)

    def test_wait_confirm_forces_conservative_edge_even_for_aggressive(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_mode": "wait_confirm",
            "entries": {
                "aggressive": {"range": {"min": 99.0, "max": 101.0}},
                "neutral": {"range": {"min": 90.0, "max": 95.0}},
            },
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_aggressive"]), 99.0, places=8)

    def test_fallback_to_existing_entry_price_when_no_ranges(self) -> None:
        d = {
            "symbol": "BTC/USDT",
            "side": "long",
            "entry_price_neutral": 123.0,
        }
        get_signal_json.apply_entry_prices_from_ranges(d)
        self.assertAlmostEqual(float(d["entry_price_neutral"]), 123.0, places=8)


if __name__ == "__main__":
    unittest.main()
