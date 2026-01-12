import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json
import postprocess_full_last


class TestEMAFanAlwaysComputed(unittest.TestCase):
    def test_ema_fan_states_and_blocks_are_computed(self) -> None:
        closes_15m = [float(i) for i in range(1, 221)]
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
            closes = closes_15m if timeframe == "15m" else closes_1h
            return {
                "market": market,
                "symbol": "SOL/USDT:USDT",
                "timeframe": timeframe,
                "last_candle_ts": 1700000000000,
                "last_candle": {
                    "ts": 1700000000000,
                    "open": float(closes[-1] - 1),
                    "high": float(closes[-1] + 1),
                    "low": float(closes[-1] - 2),
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
                    "ema_fan_m15_state": None,
                    "ema_fan_h1_state": None,
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
                self.assertIsInstance(out.get("ema_m15"), dict)
                self.assertIsInstance(out.get("ema_h1"), dict)
                self.assertIsInstance(out.get("ema_fan_m15_state"), str)
                self.assertIsInstance(out.get("ema_fan_h1_state"), str)
                self.assertIn(out.get("ema_fan_m15_state"), ("bull", "bear", "mixed"))
                self.assertIn(out.get("ema_fan_h1_state"), ("bull", "bear", "mixed"))
                self.assertIsInstance(out.get("ema_m15", {}).get("ema9"), float)
                self.assertIsInstance(out.get("ema_m15", {}).get("ema12"), float)
                self.assertIsInstance(out.get("ema_m15", {}).get("ema20"), float)
                self.assertIsInstance(out.get("ema_m15", {}).get("ema50"), float)
                self.assertIsInstance(out.get("ema_h1", {}).get("ema9"), float)
                self.assertIsInstance(out.get("ema_h1", {}).get("ema12"), float)
                self.assertIsInstance(out.get("ema_h1", {}).get("ema20"), float)
                self.assertIsInstance(out.get("ema_h1", {}).get("ema50"), float)
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]
            postprocess_full_last.BASE = old_base

    def test_insufficient_ohlcv_forces_no_trade(self) -> None:
        closes_15m = [float(i) for i in range(1, 200)]  # 199 candles: insufficient
        closes_1h = [float(i) for i in range(1001, 1200)]

        old_fetch = get_signal_json._fetch_closes_from_market
        old_base = postprocess_full_last.BASE

        def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
            closes = closes_15m if timeframe == "15m" else closes_1h
            return {
                "market": market,
                "symbol": "SOL/USDT:USDT",
                "timeframe": timeframe,
                "last_candle_ts": 1700000000000,
                "last_candle": {
                    "ts": 1700000000000,
                    "open": float(closes[-1] - 1),
                    "high": float(closes[-1] + 1),
                    "low": float(closes[-1] - 2),
                    "close": float(closes[-1]),
                    "volume": 123.0,
                },
                "ohlcv_count": len(closes),
                "fetched_at": 0.0,
                "ohlcv_tail": [],
                "closes": closes,
                "closes_tail": closes,
            }

        get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
                postprocess_full_last.BASE = Path(td)
                (Path(td) / "logs").mkdir(parents=True, exist_ok=True)

                in_data = {
                    "symbol": "SOL/USDT",
                    "price": 50.0,
                    "ema_fan_m15_state": None,
                    "ema_fan_h1_state": None,
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
                self.assertTrue(bool(out.get("no_trade")))
                self.assertIn("insufficient_ohlcv_for_ema_fan", out.get("no_trade_reasons") or [])
                self.assertIn(out.get("ema_fan_m15_state"), ("bull", "bear", "mixed"))
                self.assertIn(out.get("ema_fan_h1_state"), ("bull", "bear", "mixed"))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]
            postprocess_full_last.BASE = old_base


if __name__ == "__main__":
    unittest.main()

