import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json
import postprocess_full_last


class TestEMAProvenanceH1(unittest.TestCase):
    def test_last_json_has_h1_provenance_fields(self) -> None:
        calls: list[tuple[str, str]] = []
        closes_15m = [float(i) for i in range(1, 221)]  # >=200 candles required for EMA fan computation
        closes_1h = [float(i) for i in range(1001, 1221)]

        old_fetch = get_signal_json._fetch_closes_from_market
        old_base = postprocess_full_last.BASE

        def _ohlcv_tail_from_closes(closes: list[float]) -> list[list[float]]:
            base_ts = 1700000000000
            out: list[list[float]] = []
            tail = closes[-200:]
            for i, c in enumerate(tail):
                ts = base_ts + i * 60_000
                out.append([ts, c - 1.0, c + 1.0, c - 2.0, c, 123.0])
            return out

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            calls.append((market, timeframe))
            self.assertEqual(symbol, "SOL/USDT")
            self.assertIn(timeframe, ("15m", "1h"))
            closes = closes_15m if timeframe == "15m" else closes_1h
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
                    "close": float(closes[-1]),
                    "volume": 123.0,
                },
                "ohlcv_count": len(closes),
                "fetched_at": 0.0,
                "ohlcv_tail": _ohlcv_tail_from_closes(closes),
                "closes": closes,
                "closes_tail": closes[-200:],
            }

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
                postprocess_full_last.BASE = Path(td)
                (Path(td) / "logs").mkdir(parents=True, exist_ok=True)

                in_data = {
                    "symbol": "SOL/USDT",
                    "price": 50.0,
                    "ema20_h1": 40.0,  # intentionally conflicting: must be overwritten by provenance
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

                # Expected EMA20 from closes, SMA seed + iterative EMA (same as get_signal_json._ema_sma_seed).
                p = 20
                seed = sum(closes_1h[:p]) / float(p)
                k = 2.0 / (float(p) + 1.0)
                ema = float(seed)
                for v in closes_1h[p:]:
                    ema = float(v) * k + ema * (1.0 - k)
                self.assertAlmostEqual(float(out.get("ema20_h1")), round(ema, 6), places=6)
                self.assertEqual(out.get("timeframe_h1"), "1h")
                self.assertEqual(out.get("candles_h1_count"), len(closes_1h))
                self.assertIsInstance(out.get("last_candle_h1"), dict)
                self.assertIsNotNone(out.get("last_candle_h1", {}).get("close"))
                self.assertIsInstance(out.get("closes_h1_tail"), list)
                self.assertEqual(len(out.get("closes_h1_tail") or []), 200)
                self.assertIsInstance(out.get("ohlcv_h1_tail"), list)
                self.assertEqual(len(out.get("ohlcv_h1_tail") or []), 200)

                # Ensure we did fetch both timeframes and preferred bybit first.
                self.assertTrue(any(tf == "1h" for (_m, tf) in calls))
                self.assertTrue(any(m == "bybit_swap" for (m, _tf) in calls))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]
            postprocess_full_last.BASE = old_base


if __name__ == "__main__":
    unittest.main()
