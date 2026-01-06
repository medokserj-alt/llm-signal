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


class TestNeutralSemanticShaping(unittest.TestCase):
    def test_neutral_too_close_adjusts_and_exposes_aggressive_option(self) -> None:
        env_min = 99.6
        env_max = 100.0
        price = 100.0

        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": price,
            "side": "long",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": env_min, "max": env_max}},
                "aggressive": {"enabled": True, "range": {"min": 99.9, "max": 100.0}},
                "conservative": {"enabled": True, "range": {"min": 99.0, "max": 99.5}},
            },
            "entry_range": {"min": env_min, "max": env_max},
            "entry_price_neutral": (env_min + env_max) / 2.0,
            "entry_price_aggressive": 99.95,
            "sl_by_mode": {"neutral": 98.5},
            "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertEqual(out.get("mode"), "neutral")
        self.assertTrue(out.get("neutral_adjusted"))
        self.assertEqual(out.get("neutral_adjust_reason"), "neutral_too_close")

        er = out.get("entry_range") or {}
        er_min = float(er["min"])
        er_max = float(er["max"])
        self.assertGreaterEqual(er_min, env_min)
        self.assertLessEqual(er_max, env_max)

        entry_mid = (er_min + er_max) / 2.0
        dist_pct = abs(entry_mid - price) / price * 100.0
        self.assertGreaterEqual(dist_pct, get_signal_json.NEUTRAL_MIN_DIST_PCT_MAJOR)

        aggressive_option = out.get("aggressive_option")
        self.assertIsInstance(aggressive_option, dict)
        self.assertIn("note", aggressive_option)
        self.assertIn("aggressive", aggressive_option.get("note") or "")
        self.assertIsNotNone(aggressive_option.get("entry_price"))
        self.assertGreater(float(aggressive_option.get("entry_price")), float(out.get("entry_price_neutral")))

    def test_neutral_entry_never_closer_than_aggressive(self) -> None:
        env_min = 99.6
        env_max = 100.0
        price = 100.0

        d = {
            "no_trade": False,
            "mode": "neutral",
            "symbol": "BTC/USDT",
            "price": price,
            "side": "long",
            "entries": {
                "neutral": {"enabled": True, "range": {"min": env_min, "max": env_max}},
                "aggressive": {"enabled": True, "range": {"min": 99.65, "max": 99.75}},
                "conservative": {"enabled": True, "range": {"min": 99.0, "max": 99.5}},
            },
            "entry_range": {"min": env_min, "max": env_max},
            "entry_price_neutral": 99.8,
            "entry_price_aggressive": 99.7,
            "sl_by_mode": {"neutral": 98.5},
            "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
            "rr_by_mode": {"neutral": 1.0},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = get_signal_json.validate_active_mode_setup(d)
        self.assertEqual(out.get("mode"), "neutral")
        # If entries are inverted (neutral not more conservative than aggressive), do not show an aggressive option.
        self.assertNotIn("aggressive_option", out)

    def test_renderer_renders_aggressive_option_line(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_range": {"min": 99.6, "max": 99.9},
            "entry_price_neutral": 99.75,
            "sl_by_mode": {"neutral": 98.5},
            "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
            "aggressive_option": {"entry_price": 99.95},
        }

        out = _render_text(d)
        self.assertIn("⚡ Возможен агрессивный вход:", out)
        self.assertIn("(повышенный риск).", out)
        self.assertIn("ℹ️ Neutral-вход выставлен с запасом относительно aggressive.", out)
        self.assertNotRegex(out, r"\\d\\s*–\\s*\\d")


if __name__ == "__main__":
    unittest.main()
