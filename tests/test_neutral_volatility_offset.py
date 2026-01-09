import unittest

import get_signal_json


def _make_ohlcv_tail(*, start_price: float, tr: float, n: int = 20) -> list[list[float]]:
    out: list[list[float]] = []
    px = float(start_price)
    half = float(tr) / 2.0
    for i in range(n):
        o = px
        h = o + half
        l = o - half
        c = o
        out.append([float(i), float(o), float(h), float(l), float(c), 0.0])
        px = c
    return out


class TestNeutralVolatilityOffset(unittest.TestCase):
    def test_major_atr_moves_neutral_farther(self) -> None:
        px = 2000.0
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "ETH/USDT",
            "price": px,
            "side": "long",
            "ohlcv_m15_tail": _make_ohlcv_tail(start_price=px, tr=20.0),
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_range": {"min": 1970.0, "max": 1999.0},
            "entry_price_neutral": 1995.0,
            "entry_price_aggressive": 1998.0,
            "sl_by_mode": {"neutral": 1950.0},
            "tp_by_mode": {"neutral": {"tvh1": 2100.0, "tvh2": 2200.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertAlmostEqual(float(out.get("atr14_m15")), 20.0, places=6)
        self.assertAlmostEqual(float(out.get("neutral_offset_abs")), 24.0, places=6)
        self.assertAlmostEqual(float(out.get("neutral_offset_pct")), 1.2, places=6)

        new_entry = float(out["entry_price_neutral"])
        self.assertLessEqual(new_entry, px - 24.0)
        self.assertGreater(abs(px - new_entry), abs(px - 1995.0))

    def test_low_atr_still_respects_min_pct_major(self) -> None:
        px = 2000.0
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "ETH/USDT",
            "price": px,
            "side": "long",
            "ohlcv_m15_tail": _make_ohlcv_tail(start_price=px, tr=1.0),
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_range": {"min": 1980.0, "max": 1999.0},
            "entry_price_neutral": 1996.0,
            "entry_price_aggressive": 1998.0,
            "sl_by_mode": {"neutral": 1950.0},
            "tp_by_mode": {"neutral": {"tvh1": 2100.0, "tvh2": 2200.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertFalse(bool(out.get("no_trade")))
        self.assertAlmostEqual(float(out.get("atr14_m15")), 1.0, places=6)
        self.assertAlmostEqual(float(out.get("neutral_offset_abs")), px * 0.008, places=6)

        new_entry = float(out["entry_price_neutral"])
        self.assertLessEqual(new_entry, px - px * 0.008)

    def test_offset_crossing_sl_becomes_no_trade_and_suggests_aggressive(self) -> None:
        px = 2000.0
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "ETH/USDT",
            "price": px,
            "side": "long",
            "ohlcv_m15_tail": _make_ohlcv_tail(start_price=px, tr=1.0),
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_range": {"min": 1980.0, "max": 1999.0},
            "entry_price_neutral": 1998.0,
            "entry_price_aggressive": 1999.0,
            "sl_by_mode": {"neutral": 1995.0},
            "tp_by_mode": {"neutral": {"tvh1": 2100.0, "tvh2": 2200.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("neutral_no_good_entry_volatility", out.get("no_trade_reasons") or [])
        self.assertIsNone(out.get("entry_price_neutral"))
        self.assertIsNone((out.get("sl_by_mode") or {}).get("neutral"))
        self.assertIsNone((out.get("tp_by_mode") or {}).get("neutral"))
        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertIsNotNone(aggressive_option.get("entry_price"))

    def test_offset_outside_entry_range_is_skipped_with_warning(self) -> None:
        px = 2000.0
        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "ETH/USDT",
            "price": px,
            "side": "long",
            "ohlcv_m15_tail": _make_ohlcv_tail(start_price=px, tr=20.0),
            "entries": {"neutral": {"enabled": True}, "aggressive": {"enabled": True}},
            "entry_range": {"min": 1997.0, "max": 1999.0},
            "entry_price_neutral": 1998.5,
            "entry_price_aggressive": 1999.5,
            "sl_by_mode": {"neutral": 1950.0},
            "tp_by_mode": {"neutral": {"tvh1": 2100.0, "tvh2": 2200.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("neutral_no_good_entry_volatility", out.get("no_trade_reasons") or [])
        self.assertIsNone(out.get("entry_price_neutral"))
        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertIsNotNone(aggressive_option.get("entry_price"))


if __name__ == "__main__":
    unittest.main()
