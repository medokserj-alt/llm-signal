import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json
import postprocess_full_last


class TestEMAProvenanceM15(unittest.TestCase):
    def test_last_json_has_provenance_and_ema_uses_15m(self) -> None:
        calls: list[tuple[str, str]] = []
        closes = [float(i) for i in range(1, 31)]  # 30 closes, enough for EMA20 with warm-up tail

        old_fetch = get_signal_json._fetch_closes_from_market
        old_base = postprocess_full_last.BASE

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            calls.append((market, timeframe))
            # Regression: EMA20_m15 must be built from 15m candles, not 1h/5m/etc.
            self.assertEqual(timeframe, "15m")
            self.assertEqual(symbol, "SOL/USDT")
            return {
                "market": market,
                "symbol": "SOL/USDT:USDT",
                "timeframe": timeframe,
                "last_candle_ts": 1700000000000,
                "last_candle": {
                    "ts": 1700000000000,
                    "open": 29.0,
                    "high": 30.0,
                    "low": 28.0,
                    "close": 30.0,
                    "volume": 123.0,
                },
                "ohlcv_count": 30,
                "fetched_at": 0.0,
                "closes": closes,
            }

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
                postprocess_full_last.BASE = Path(td)
                (Path(td) / "logs").mkdir(parents=True, exist_ok=True)

                in_data = {
                    "symbol": "SOL/USDT",
                    "price": 50.0,
                    "ema20_h1": 40.0,  # avoid any network fetch for H1
                    "entries": {"neutral": {"enabled": True, "range": {"min": 1, "max": 2}}},
                    "no_trade": False,
                    "no_trade_reasons": [],
                    "no_trade_hint": "",
                    "mode": "neutral",
                }
                (Path(td) / "logs" / "last.json").write_text(
                    json.dumps(in_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                postprocess_full_last.main()

                out = json.loads((Path(td) / "logs" / "last.json").read_text(encoding="utf-8"))

                self.assertEqual(out.get("exchange"), "bybit")
                self.assertEqual(out.get("market_type"), "linear_perp")
                self.assertEqual(out.get("price_source"), "last")
                self.assertEqual(out.get("timeframe_m15"), "15m")
                self.assertEqual(out.get("candles_m15_count"), 30)
                self.assertIsInstance(out.get("last_candle_m15"), dict)
                self.assertIsNotNone(out.get("last_candle_m15", {}).get("close"))
                self.assertIsInstance(out.get("closes_m15_tail"), list)
                self.assertEqual(len(out.get("closes_m15_tail") or []), 5)

                # Expected EMA20 from closes, SMA seed + iterative EMA (same as get_signal_json._ema_sma_seed).
                p = 20
                seed = sum(closes[:p]) / float(p)
                k = 2.0 / (float(p) + 1.0)
                ema = float(seed)
                for v in closes[p:]:
                    ema = float(v) * k + ema * (1.0 - k)
                self.assertAlmostEqual(float(out.get("ema20_m15")), round(ema, 6), places=6)

                # Ensure we did fetch and only needed the 15m path.
                self.assertTrue(any(m == "bybit_swap" for (m, _tf) in calls))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]
            postprocess_full_last.BASE = old_base


if __name__ == "__main__":
    unittest.main()

