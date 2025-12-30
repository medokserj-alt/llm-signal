import unittest

import get_signal_json


class TestEMALLMOverride(unittest.TestCase):
    def test_finalize_signal_overwrites_llm_ema_with_provenance(self) -> None:
        calls: list[tuple[str, str]] = []
        closes_15m = [float(i) for i in range(1, 31)]
        closes_1h = [float(i) for i in range(101, 131)]

        old_fetch = get_signal_json._fetch_closes_from_market

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            calls.append((market, timeframe))
            self.assertEqual(market, "bybit_swap")
            self.assertEqual(symbol, "ETH/USDT")
            self.assertIn(timeframe, ("15m", "1h"))
            closes = closes_15m if timeframe == "15m" else closes_1h
            return {
                "market": market,
                "symbol": "ETH/USDT:USDT",
                "timeframe": timeframe,
                "last_candle_ts": 1700000000000,
                "last_candle": {
                    "ts": 1700000000000,
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": float(closes[-1]),
                    "volume": 123.0,
                },
                "ohlcv_count": len(closes),
                "fetched_at": 0.0,
                "closes": closes,
            }

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            llm_data = {
                "symbol": "ETH/USDT",
                "price": 50.0,
                # Intentionally conflicting values from LLM output: must not survive.
                "ema20_m15": 888.0,
                "ema20_h1": 999.0,
                "direction": "long",
                "entry_mode": "limit",
                "entry_range": {"min": 1, "max": 2},
            }
            out = get_signal_json.finalize_signal(llm_data, hints={}, fetch_price=False)

            # Expected EMA20 from closes, SMA seed + iterative EMA (same as get_signal_json._ema_sma_seed).
            p = 20
            seed_15m = sum(closes_15m[:p]) / float(p)
            k = 2.0 / (float(p) + 1.0)
            ema_15m = float(seed_15m)
            for v in closes_15m[p:]:
                ema_15m = float(v) * k + ema_15m * (1.0 - k)

            seed_1h = sum(closes_1h[:p]) / float(p)
            ema_1h = float(seed_1h)
            for v in closes_1h[p:]:
                ema_1h = float(v) * k + ema_1h * (1.0 - k)

            self.assertAlmostEqual(float(out.get("ema20_m15")), round(ema_15m, 6), places=6)
            self.assertAlmostEqual(float(out.get("ema20_h1")), round(ema_1h, 6), places=6)

            self.assertEqual(out.get("exchange"), "bybit")
            self.assertEqual(out.get("market_type"), "linear_perp")
            self.assertEqual(out.get("price_source"), "last")

            self.assertIsInstance(out.get("last_candle_m15"), dict)
            self.assertIsInstance(out.get("last_candle_h1"), dict)

            # Relation flags must match computed EMA values.
            self.assertEqual(out.get("price_vs_ema20_m15"), "above")
            self.assertEqual(out.get("price_vs_ema20_h1"), "below")

            # Both timeframes should be fetched.
            self.assertTrue(any(tf == "15m" for (_m, tf) in calls))
            self.assertTrue(any(tf == "1h" for (_m, tf) in calls))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()

