import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import get_signal_json
import postprocess_full_last


class TestAggressiveCountertrendWarningPreservedInLastJson(unittest.TestCase):
    def test_countertrend_wait_confirm_warning_survives_runtime_like_pipeline(self) -> None:
        def _patch_fetch_closes(closes_15m: list[float], closes_1h: list[float]):
            old = get_signal_json._fetch_closes_from_market
            required = 200

            def _pad(closes: list[float]) -> list[float]:
                if not closes:
                    return closes
                if len(closes) >= required:
                    return closes
                return [float(closes[0])] * (required - len(closes)) + [float(x) for x in closes]

            def _ohlcv_tail(closes: list[float]) -> list[list[float]]:
                base_ts = 1700000000000
                tail = closes[-required:]
                out: list[list[float]] = []
                for i, c in enumerate(tail):
                    ts = base_ts + i * 60_000
                    out.append([ts, c - 1.0, c + 1.0, c - 2.0, c, 1.0])
                return out

            def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
                closes = closes_15m if timeframe == "15m" else (closes_1h if timeframe == "1h" else None)
                if closes is None:
                    raise AssertionError(f"Unexpected timeframe: {timeframe}")
                closes = _pad(closes)
                return {
                    "market": market,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "last_candle_ts": None,
                    "last_candle": None,
                    "ohlcv_count": len(closes),
                    "ohlcv_tail": _ohlcv_tail(closes),
                    "fetched_at": 0.0,
                    "closes": closes,
                    "closes_tail": closes[-required:],
                }

            get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
            return old

        # Provide deterministic OHLCV tails for provenance-based EMA20 (finalize_signal)
        # and for EMA blocks (postprocess_full_last).
        closes_15m = [100.0] * 218 + [101.0, 101.0]
        closes_1h = [50.0 + i * 0.01 for i in range(220)]
        old_fetch = _patch_fetch_closes(closes_15m, closes_1h)
        try:
            d = {
                "no_trade": False,
                "mode": "aggressive",
                "symbol": "BTC/USDT",
                "price": 99.0,
                "side": "short",
                "entry_mode": "limit",
                # Provide mode buckets directly; keep `entry_range` absent so build_entries() does not
                # override/disable the aggressive bucket in this unit-test environment.
                "entries": {
                    "aggressive": {"enabled": True, "range": {"min": 99.0, "max": 101.0}},
                    # Keep non-active buckets minimal so EMA exhale filter doesn't infer a neutral entry_ref
                    # (and thus doesn't disable aggressive due to between-EMA heuristics).
                    "neutral": {"enabled": True},
                    "conservative": {"enabled": True},
                },
                "entry_price_aggressive": 100.0,
                "sl_by_mode": {"aggressive": 105.0},
                "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0, "tvh3": 85.0}},
                "rr_by_mode": {"aggressive": 2.0},
                "exit_plan_by_mode": {"aggressive": "plan"},
                # Regression: runtime can surface non-list warnings (LLM / upstream artifact).
                "warnings": "",
                "ema_fan_h1_state": "bull",
                "ema_fan_m15_state": "bull",
                "phase_flip_m15": False,
                "impulse_proxy": False,
            }

            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)
            self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "wait_confirm")
            self.assertIn(
                "aggressive_countertrend_no_evidence_wait_confirm",
                out.get("warnings") or [],
            )

            with TemporaryDirectory() as td:
                base = Path(td)
                (base / "logs").mkdir(parents=True, exist_ok=True)
                (base / "logs" / "last.json").write_text(
                    json.dumps(out, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                old_base = postprocess_full_last.BASE
                try:
                    postprocess_full_last.BASE = base
                    postprocess_full_last.main()
                finally:
                    postprocess_full_last.BASE = old_base

                out2 = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
                self.assertIn(
                    "aggressive_countertrend_no_evidence_wait_confirm",
                    out2.get("warnings") or [],
                )
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
