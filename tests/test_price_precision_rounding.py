import unittest

import get_signal_json


class TestPricePrecisionRounding(unittest.TestCase):
    def test_xrp_entry_prices_keep_4_decimals(self) -> None:
        d = {
            "symbol": "XRP/USDT",
            "price": 1.87,
            "direction": "long",
            "entry_mode": "limit",
            "entry_range": {"min": 1.8688, "max": 1.8699},
        }
        get_signal_json.apply_entry_prices_from_ranges(d)

        self.assertEqual(f"{float(d['entry_price_neutral']):.4f}", "1.8688")
        self.assertEqual(f"{float(d['entry_price_aggressive']):.4f}", "1.8699")

    def test_btc_rounding_stays_2_decimals(self) -> None:
        self.assertEqual(get_signal_json._round_price(100.1299, symbol="BTC/USDT"), 100.13)

    def test_xrp_top_level_sl_tp_match_neutral_mode_bucket(self) -> None:
        closes = [1.87] * 40
        old_fetch = get_signal_json._fetch_closes_from_market

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            self.assertEqual(market, "bybit_swap")
            self.assertEqual(symbol, "XRP/USDT")
            self.assertIn(timeframe, ("15m", "1h"))
            return {
                "market": market,
                "symbol": "XRP/USDT:USDT",
                "timeframe": timeframe,
                "last_candle_ts": 1700000000000,
                "last_candle": {
                    "ts": 1700000000000,
                    "open": float(closes[-1]),
                    "high": float(closes[-1]),
                    "low": float(closes[-1]),
                    "close": float(closes[-1]),
                    "volume": 123.0,
                },
                "ohlcv_count": len(closes),
                "fetched_at": 0.0,
                "closes": closes,
            }

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            d = {
                "no_trade": False,
                "time_msk": "01.01.2025, 12:00",
                "symbol": "XRP/USDT",
                "price": 1.87,
                "direction": "long",
                "entry_mode": "limit",
                "entry_range": {"min": 1.8450, "max": 1.8550},
                "entry_price_neutral": 1.8450,
                "entry_price_aggressive": 1.8550,
                "sl": 1.84,
                "tp1": 1.87,
                "tp2": 1.9,
                "entries": {
                    "neutral": {"enabled": True, "range": {"min": 1.8450, "max": 1.8550}},
                    "aggressive": {"enabled": True},
                },
                "sl_by_mode": {"neutral": 1.8264},
                "tp_by_mode": {"neutral": {"tvh1": 1.9, "tvh2": 1.93}},
                "rr_by_mode": {"neutral": 1.0},
                "exit_plan_by_mode": {"neutral": "plan"},
            }
            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)

            self.assertEqual(out.get("mode"), "neutral")
            self.assertEqual(f"{float(out['sl']):.4f}", "1.8264")
            self.assertEqual(float(out["tp1"]), 1.9)
            self.assertEqual(float(out["tp2"]), 1.93)
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]

if __name__ == "__main__":
    unittest.main()
