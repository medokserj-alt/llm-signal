import unittest

import get_signal_json


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


class TestNeutralFlipWithoutReclaimGate(unittest.TestCase):
    def test_full_like_validate_blocks_neutral_and_exposes_aggressive_option(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "phase_flip_m15": True,
            "impulse_proxy": True,
            "price_vs_ema20_m15": "below",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 98.0, "max": 99.0}},
                "aggressive": {"enabled": True, "range": {"min": 99.2, "max": 99.6}},
            },
            "entry_range": {"min": 98.0, "max": 99.0},
            "entry_price_neutral": 98.5,
            "entry_price_aggressive": 99.4,
            "sl_by_mode": {"neutral": 97.0},
            "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
            "rr_by_mode": {"neutral": 1.6},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("neutral_flip_without_reclaim_forbidden", out.get("no_trade_reasons") or [])
        self.assertIsNone(out.get("entry_price_neutral"))
        self.assertNotIn("neutral", (out.get("sl_by_mode") or {}))
        self.assertNotIn("neutral", (out.get("tp_by_mode") or {}))

        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertIsNotNone(aggressive_option.get("entry_price"))
        self.assertEqual(
            aggressive_option.get("note"),
            "Разворот после импульса (phase flip) без закрепления выше EMA20(M15): "
            "neutral запрещён; допустимо только в aggressive (лучше wait_confirm).",
        )

    def test_full_like_validate_allows_when_reclaimed_above(self) -> None:
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "phase_flip_m15": True,
            "impulse_proxy": True,
            "price_vs_ema20_m15": "above",
            "price_vs_ema20_h1": "above",
            "ema_fan_h1_state": "bull",
            "ema_fan_m15_state": "bull",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 98.0, "max": 99.0}},
                "aggressive": {"enabled": True, "range": {"min": 99.2, "max": 99.6}},
            },
            "entry_range": {"min": 98.0, "max": 99.0},
            "entry_price_neutral": 98.5,
            "entry_price_aggressive": 99.4,
            "sl_by_mode": {"neutral": 97.0},
            "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
            "rr_by_mode": {"neutral": 1.6},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertNotIn("neutral_flip_without_reclaim_forbidden", out.get("no_trade_reasons") or [])
        self.assertIsNotNone(out.get("entry_price_neutral"))

    def test_single_like_finalize_blocks_with_flags_and_below(self) -> None:
        closes_15m = [100.0] * 58 + [99.0, 99.0]
        closes_1h = [200.0] * 60
        old_fetch = _patch_fetch_closes(closes_15m, closes_1h)
        try:
            d = {
                "time_msk": "01.01.2025, 12:00",
                "symbol": "BTC/USDT",
                "price": 99.0,
                "side": "long",
                "mode": "neutral",
                "warnings": ["impulse_no_exhale"],
                "ema20_m15": 100.0,
                "price_vs_ema20_m15": "below",
                "entry_range": {"min": 98.0, "max": 99.0},
                "entry_price_neutral": 98.5,
                "entry_price_aggressive": 99.4,
                "entries": {
                    "neutral": {"enabled": True, "range": {"min": 98.0, "max": 99.0}},
                    "aggressive": {"enabled": True, "range": {"min": 99.2, "max": 99.6}},
                },
                "sl_by_mode": {"neutral": 97.0},
                "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
                "rr_by_mode": {"neutral": 1.6},
                "exit_plan_by_mode": {"neutral": "plan"},
            }

            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)

            self.assertTrue(bool(out.get("no_trade")))
            self.assertIn("neutral_flip_without_reclaim_forbidden", out.get("no_trade_reasons") or [])
            self.assertIsNone(out.get("entry_price_neutral"))
            self.assertIsInstance(out.get("aggressive_option"), dict)
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
