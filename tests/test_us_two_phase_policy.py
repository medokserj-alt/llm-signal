import contextlib
import io
import json
import unittest
from unittest.mock import patch

import get_signal_json
import render_strict


def _render_text(data: dict) -> str:
    buf_out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data))),
        patch("render_strict.pathlib.Path.write_text", return_value=None),
        contextlib.redirect_stdout(buf_out),
    ):
        render_strict.main()
    return buf_out.getvalue()


class TestUsTwoPhasePolicy(unittest.TestCase):
    def test_neutral_phase1_is_blocked(self) -> None:
        d = {
            "time_msk": "01.01.2025, 18:00",  # 15:00 UTC => Phase 1 (17:00–19:30 MSK)
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
            "warnings": [],
        }
        get_signal_json.apply_phase_flip_modifier(d)
        get_signal_json.apply_us_two_phase_policy(d)
        get_signal_json.normalize_no_trade(d)

        self.assertTrue(bool(d.get("is_us_open_block")))
        self.assertFalse(bool(d.get("is_us_session_late")))
        self.assertTrue(bool(d.get("no_trade")))
        self.assertEqual(d.get("no_trade_hint"), "us_open_block_non_aggressive")
        self.assertIn("us_open_block_non_aggressive", d.get("no_trade_reasons") or [])

        # Strict block should not emit neutral entry/SL/TP payload.
        self.assertNotIn("entry_price_neutral", d)
        self.assertNotIn("entry_range", d)
        self.assertNotIn("sl", d)
        self.assertNotIn("tp1", d)
        self.assertNotIn("tp2", d)
        self.assertNotIn("tp3", d)
        self.assertNotIn("neutral", (d.get("sl_by_mode") or {}))
        self.assertNotIn("neutral", (d.get("tp_by_mode") or {}))

    def test_conservative_phase1_is_blocked(self) -> None:
        d = {
            "time_msk": "01.01.2025, 18:00",  # Phase 1
            "no_trade": False,
            "mode": "conservative",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "side": "long",
            "warnings": [],
            "day_mid_context": {"day_bias": "long", "mid_bias": "long", "notes": None},
            "entries": {
                "neutral": {"enabled": True, "range": {"min": 98.5, "max": 99.0}},
                "aggressive": {"enabled": True, "range": {"min": 99.4, "max": 99.7}},
                "conservative": {"enabled": True, "range": {"min": 97.5, "max": 98.0}},
            },
            "entry_range": {"min": 97.5, "max": 98.0},
            "entry_price_neutral": 98.75,
            "entry_price_aggressive": 99.55,
            "entry_price_conservative": 97.75,
            "sl_by_mode": {"conservative": 96.5},
            "tp_by_mode": {"conservative": {"tvh1": 101.0, "tvh2_or_trail": "trail"}},
            "rr_by_mode": {"conservative": 2.2},
            "exit_plan_by_mode": {"conservative": "plan"},
            "ema_fan_m15_state": "bull",
            "ema_fan_h1_state": "bull",
            "price_vs_ema20_m15": "above",
            "price_vs_ema20_h1": "above",
        }
        get_signal_json.apply_phase_flip_modifier(d)
        get_signal_json.apply_us_two_phase_policy(d)
        get_signal_json.normalize_no_trade(d)

        self.assertTrue(bool(d.get("is_us_open_block")))
        self.assertTrue(bool(d.get("no_trade")))
        self.assertEqual(d.get("no_trade_hint"), "us_open_block_non_aggressive")
        self.assertIn("us_open_block_non_aggressive", d.get("no_trade_reasons") or [])

        self.assertNotIn("entry_price_conservative", d)
        self.assertNotIn("entry_range", d)
        self.assertNotIn("sl", d)
        self.assertNotIn("tp1", d)
        self.assertNotIn("tp2", d)
        self.assertNotIn("tp3", d)
        self.assertNotIn("conservative", (d.get("sl_by_mode") or {}))
        self.assertNotIn("conservative", (d.get("tp_by_mode") or {}))

    def test_aggressive_phase1_forces_wait_confirm_and_renders_warning(self) -> None:
        d = {
            "time_msk": "01.01.2025, 18:00",  # Phase 1
            "no_trade": False,
            "symbol": "BTC/USDT",
            "price": 101.0,
            "direction": "short",
            "mode": "aggressive",
            "entry_mode": "limit",
            "entry_price_aggressive": 101.4,
            "warnings": [],
            "sl_by_mode": {"aggressive": 105.0},
            "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0, "tvh3": 85.0}},
            "rr_by_mode": {"aggressive": 2.0},
            "exit_plan_by_mode": {"aggressive": "TP1 partial; TP2; TP3 optional"},
        }
        get_signal_json.apply_phase_flip_modifier(d)
        get_signal_json.apply_us_two_phase_policy(d)
        get_signal_json.normalize_no_trade(d)

        self.assertTrue(bool(d.get("is_us_open_block")))
        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(str(d.get("entry_mode") or "").strip().lower(), "wait_confirm")

        rendered = _render_text(d)
        self.assertIn(
            "⚠️⚠️ USA OPEN (17:00–19:30 МСК): HIGH VOLATILITY / FAKE MOVES — WAIT CONFIRM ⚠️⚠️",
            rendered,
        )

    def test_phase2_does_not_render_open_warning(self) -> None:
        d = {
            "time_msk": "01.01.2025, 20:00",  # Phase 2
            "no_trade": False,
            "symbol": "BTC/USDT",
            "price": 101.0,
            "direction": "short",
            "mode": "aggressive",
            "entry_mode": "limit",
            "entry_price_aggressive": 101.4,
            "warnings": [],
            "sl_by_mode": {"aggressive": 105.0},
            "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0, "tvh3": 85.0}},
            "rr_by_mode": {"aggressive": 2.0},
            "exit_plan_by_mode": {"aggressive": "TP1 partial; TP2; TP3 optional"},
        }
        get_signal_json.apply_phase_flip_modifier(d)
        get_signal_json.apply_us_two_phase_policy(d)
        get_signal_json.normalize_no_trade(d)

        self.assertFalse(bool(d.get("is_us_open_block")))
        self.assertTrue(bool(d.get("is_us_session_late")))
        rendered = _render_text(d)
        self.assertNotIn(
            "⚠️⚠️ USA OPEN (17:00–19:30 МСК): HIGH VOLATILITY / FAKE MOVES — WAIT CONFIRM ⚠️⚠️",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
