import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json
import postprocess_full_last


class TestEMAProvenanceM15(unittest.TestCase):
    def test_last_json_has_provenance_and_ema_uses_15m(self) -> None:
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
            # Regression: EMA20_m15 must be built from 15m candles, not 1h/5m/etc.
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
                    "ema20_h1": 40.0,  # intentionally present; must still be overwritten by provenance
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
                self.assertEqual(out.get("candles_m15_count"), len(closes_15m))
                self.assertIsInstance(out.get("last_candle_m15"), dict)
                self.assertIsNotNone(out.get("last_candle_m15", {}).get("close"))
                self.assertIsInstance(out.get("closes_m15_tail"), list)
                self.assertEqual(len(out.get("closes_m15_tail") or []), 200)
                self.assertIsInstance(out.get("ohlcv_m15_tail"), list)
                self.assertEqual(len(out.get("ohlcv_m15_tail") or []), 200)

                # Expected EMA20 from closes, SMA seed + iterative EMA (same as get_signal_json._ema_sma_seed).
                p = 20
                seed = sum(closes_15m[:p]) / float(p)
                k = 2.0 / (float(p) + 1.0)
                ema = float(seed)
                for v in closes_15m[p:]:
                    ema = float(v) * k + ema * (1.0 - k)
                self.assertAlmostEqual(float(out.get("ema20_m15")), round(ema, 6), places=6)

                # Ensure we did fetch using Bybit (single source of truth).
                self.assertTrue(any(m == "bybit_swap" for (m, _tf) in calls))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]
            postprocess_full_last.BASE = old_base


if __name__ == "__main__":
    unittest.main()
