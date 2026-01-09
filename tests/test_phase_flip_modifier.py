import unittest

import get_signal_json


def _patch_fetch_closes(closes_15m: list[float], closes_1h: list[float]):
    old = get_signal_json._fetch_closes_from_market

    def fake_fetch(market: str, timeframe: str, *, symbol: str, limit: int, min_len: int):  # type: ignore[no-untyped-def]
        closes = closes_15m if timeframe == "15m" else (closes_1h if timeframe == "1h" else None)
        if closes is None:
            raise AssertionError(f"Unexpected timeframe: {timeframe}")
        return {
            "market": market,
            "symbol": symbol,
            "timeframe": timeframe,
            "last_candle_ts": None,
            "last_candle": None,  # allow US-session detection to rely on time_msk
            "ohlcv_count": len(closes),
            "ohlcv_tail": None,
            "fetched_at": 0.0,
            "closes": closes,
        }

    get_signal_json._fetch_closes_from_market = fake_fetch  # type: ignore[assignment]
    return old


class TestPhaseFlipModifier(unittest.TestCase):
    def test_phase_flip_modifier_fields_persisted_top_level(self) -> None:
        d = {
            "time_msk": "01.01.2025, 20:00",  # 17:00 UTC (US session)
            "direction": "short",
            "price_vs_ema20_m15": "above",
            "ema20_m15": 100.0,
            "closes_m15_tail": [101.0, 101.0],
            "warnings": ["impulse_no_exhale"],
        }
        get_signal_json.apply_phase_flip_modifier(d)
        for k in ("is_us_session", "impulse_proxy", "phase_flip_m15"):
            self.assertIn(k, d)
            self.assertIsInstance(d.get(k), bool)

    def test_neutral_impulse_proxy_phase_flip_us_session_does_not_autoblock(self) -> None:
        closes_15m = [100.0] * 58 + [101.0, 101.0]
        closes_1h = [200.0] * 60
        old_fetch = _patch_fetch_closes(closes_15m, closes_1h)
        try:
            d = {
                "time_msk": "01.01.2025, 20:00",  # 17:00 UTC (US session)
                "symbol": "BTC/USDT",
                "price": 101.0,
                "direction": "short",
                "mode": "neutral",
                "entry_mode": "limit",
                "entry_range": {"min": 101.2, "max": 101.6},
                "entries": {"neutral": {"enabled": True, "range": {"min": 101.2, "max": 101.6}}},
                "entry_price_neutral": 101.4,
                "sl_by_mode": {"neutral": 105.0},
                "tp_by_mode": {"neutral": {"tvh1": 99.0, "tvh2": 98.0}},
                "rr_by_mode": {"neutral": 1.2},
                "exit_plan_by_mode": {"neutral": "plan"},
                "ema20_m15": 100.0,
                "price_vs_ema20_m15": "above",
                "warnings": ["impulse_no_exhale"],
            }
            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)
            self.assertTrue(bool(out.get("no_trade")))
            self.assertIn("waiting_confirmation", out.get("no_trade_reasons") or [])
            self.assertNotIn("neutral_flip_without_reclaim_forbidden", out.get("no_trade_reasons") or [])
            dbg = out.get("debug") or {}
            self.assertTrue(bool(dbg.get("is_us_session")))
            self.assertTrue(bool(dbg.get("phase_flip_m15")))
            self.assertTrue(bool(dbg.get("impulse_proxy")))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]

    def test_neutral_impulse_proxy_phase_flip_outside_us_does_not_autoblock(self) -> None:
        closes_15m = [100.0] * 58 + [101.0, 101.0]
        closes_1h = [200.0] * 60
        old_fetch = _patch_fetch_closes(closes_15m, closes_1h)
        try:
            d = {
                "time_msk": "01.01.2025, 10:00",  # 07:00 UTC (outside US session)
                "symbol": "BTC/USDT",
                "price": 101.0,
                "direction": "short",
                "mode": "neutral",
                "entry_mode": "limit",
                "entry_range": {"min": 101.2, "max": 101.6},
                "entries": {"neutral": {"enabled": True, "range": {"min": 101.2, "max": 101.6}}},
                "entry_price_neutral": 101.4,
                "sl_by_mode": {"neutral": 105.0},
                "tp_by_mode": {"neutral": {"tvh1": 99.0, "tvh2": 98.0}},
                "rr_by_mode": {"neutral": 1.2},
                "exit_plan_by_mode": {"neutral": "plan"},
                "ema20_m15": 100.0,
                "price_vs_ema20_m15": "above",
                "warnings": ["impulse_no_exhale"],
            }
            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)
            self.assertTrue(bool(out.get("no_trade")))
            self.assertIn("waiting_confirmation", out.get("no_trade_reasons") or [])
            self.assertNotIn("neutral_flip_without_reclaim_forbidden", out.get("no_trade_reasons") or [])
            dbg = out.get("debug") or {}
            self.assertFalse(bool(dbg.get("is_us_session")))
            self.assertTrue(bool(dbg.get("phase_flip_m15")))
            self.assertTrue(bool(dbg.get("impulse_proxy")))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]

    def test_aggressive_impulse_proxy_phase_flip_forces_wait_confirm(self) -> None:
        closes_15m = [100.0] * 58 + [101.0, 101.0]
        closes_1h = [200.0] * 60
        old_fetch = _patch_fetch_closes(closes_15m, closes_1h)
        try:
            d = {
                "time_msk": "01.01.2025, 20:00",  # US session
                "symbol": "BTC/USDT",
                "price": 101.0,
                "direction": "short",
                "mode": "aggressive",
                "entry_mode": "market",
                "entry_range": {"min": 101.2, "max": 101.6},
                "warnings": ["impulse_no_exhale"],
                "sl_by_mode": {"aggressive": 105.0},
                "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0, "tvh3": 85.0}},
                "rr_by_mode": {"aggressive": 2.0},
                "exit_plan_by_mode": {"aggressive": "TP1 partial; TP2; TP3 optional"},
            }
            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)
            self.assertFalse(bool(out.get("no_trade")))
            self.assertEqual(str(out.get("entry_mode") or "").strip().lower(), "wait_confirm")
            wl = " ".join(str(w).lower() for w in (out.get("warnings") or []))
            self.assertIn("phase_flip_wait_confirm", wl)
            self.assertIn("us-сессия", wl)
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]

    def test_no_phase_flip_behavior_unchanged(self) -> None:
        closes_15m = [100.0] * 60
        closes_1h = [200.0] * 60
        old_fetch = _patch_fetch_closes(closes_15m, closes_1h)
        try:
            d = {
                "time_msk": "01.01.2025, 12:00",
                "symbol": "BTC/USDT",
                "price": 99.0,  # below ema20_m15 -> no phase_flip for SHORT
                "direction": "short",
                "mode": "aggressive",
                "entry_mode": "limit",
                "entry_range": {"min": 99.2, "max": 99.6},
                "warnings": ["impulse_no_exhale"],
                "sl_by_mode": {"aggressive": 105.0},
                "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0, "tvh3": 85.0}},
                "rr_by_mode": {"aggressive": 2.0},
                "exit_plan_by_mode": {"aggressive": "TP1 partial; TP2; TP3 optional"},
            }
            out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)
            wl = " ".join(str(w).lower() for w in (out.get("warnings") or []))
            self.assertNotIn("phase_flip_wait_confirm", wl)
            dbg = out.get("debug") or {}
            self.assertFalse(bool(dbg.get("phase_flip_m15")))
        finally:
            get_signal_json._fetch_closes_from_market = old_fetch  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
