import unittest


import get_signal_json


class TestSinglePriceHintAndScale(unittest.TestCase):
    def test_apply_live_price_hint_sets_price(self) -> None:
        old_get_pair_ticker = get_signal_json.get_pair_ticker
        try:
            get_signal_json.get_pair_ticker = lambda sym: {"last": 2.01, "change": None}  # type: ignore[assignment]
            payload = {"hints": {"symbol": "XRP/USDT"}}
            get_signal_json.apply_live_price_hint(payload)
            self.assertEqual(payload["hints"].get("price"), 2.01)
            self.assertEqual(payload["hints"].get("price_source"), "live")
        finally:
            get_signal_json.get_pair_ticker = old_get_pair_ticker  # type: ignore[assignment]

    def test_entry_range_and_entry_prices_stay_on_price_scale(self) -> None:
        d = {
            "symbol": "XRP/USDT",
            "price": 2.0,
            "direction": "long",
            "entry_mode": "limit",
            "entry_range": {"min": 1.98, "max": 2.0},
        }
        get_signal_json.build_entries(d)
        get_signal_json.apply_entry_prices_from_ranges(d)

        price = float(d["price"])
        er = d.get("entry_range") or {}
        mid = (float(er["min"]) + float(er["max"])) / 2.0
        self.assertGreater(mid, 0.0)
        self.assertLess(mid / price, 2.0)
        self.assertGreater(mid / price, 0.5)

        for k in ("entry_price_neutral", "entry_price_aggressive", "entry_price_conservative"):
            self.assertIsNotNone(d.get(k))
            v = float(d.get(k))
            self.assertLess(v / price, 2.0)
            self.assertGreater(v / price, 0.5)


if __name__ == "__main__":
    unittest.main()

