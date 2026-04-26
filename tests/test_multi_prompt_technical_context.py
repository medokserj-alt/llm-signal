import unittest

import get_signal_json


def _make_ohlcv(closes: list[float]) -> list[list[float]]:
    base_ts = 1700000000000
    out: list[list[float]] = []
    for i, close in enumerate(closes[-200:]):
        ts = base_ts + i * 60_000
        out.append([ts, close - 1.0, close + 2.0, close - 2.0, close, 100.0 + i])
    return out


class TestMultiPromptTechnicalContext(unittest.TestCase):
    def test_build_symbol_prompt_technical_context_exposes_ema_and_structure(self) -> None:
        old_fetch = get_signal_json._fetch_closes_from_market

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            self.assertEqual(market, "bybit_swap")
            self.assertEqual(symbol, "BNB/USDT")
            closes_by_tf = {
                "15m": [float(i) for i in range(1, 241)],
                "1h": [float(i) for i in range(301, 541)],
                "4h": [float(i) for i in range(601, 841)],
            }
            closes = closes_by_tf[timeframe]
            return {
                "market": market,
                "symbol": "BNB/USDT:USDT",
                "timeframe": timeframe,
                "last_candle": {
                    "ts": 1700000000000,
                    "open": closes[-1] - 1.0,
                    "high": closes[-1] + 2.0,
                    "low": closes[-1] - 2.0,
                    "close": closes[-1],
                    "volume": 123.0,
                },
                "ohlcv_tail": _make_ohlcv(closes),
                "closes": closes,
            }

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            out = get_signal_json.build_symbol_prompt_technical_context("BNB/USDT", price=640.0)
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]

        self.assertEqual(out["symbol"], "BNB/USDT")
        self.assertEqual(out["price"], 640.0)
        self.assertEqual(set(out["timeframes"].keys()), {"15m", "1h", "4h"})
        self.assertIsNotNone(out["timeframes"]["15m"]["ema20"])
        self.assertIsNotNone(out["timeframes"]["15m"]["ema60"])
        self.assertIn("recent_high", out["timeframes"]["15m"]["structure_levels"])
        self.assertIn("recent_low", out["timeframes"]["15m"]["structure_levels"])
        self.assertIn(out["timeframes"]["15m"]["price_vs_ema20"], {"above", "below", "equal"})

    def test_build_pool_prompt_technical_context_skips_assets_without_ohlcv(self) -> None:
        old_fetch = get_signal_json._fetch_closes_from_market

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            if symbol == "BTC/USDT":
                closes = [float(i) for i in range(1, 241)]
                return {
                    "market": market,
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": timeframe,
                    "last_candle": {"ts": 1700000000000, "open": 1.0, "high": 2.0, "low": 0.5, "close": closes[-1], "volume": 1.0},
                    "ohlcv_tail": _make_ohlcv(closes),
                    "closes": closes,
                }
            return None

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            out = get_signal_json.build_pool_prompt_technical_context(
                {
                    "BTC/USDT": {"last": 25000.0},
                    "ETH/USDT": {"last": 1500.0},
                }
            )
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]

        self.assertIn("BTC/USDT", out)
        self.assertNotIn("ETH/USDT", out)


if __name__ == "__main__":
    unittest.main()
