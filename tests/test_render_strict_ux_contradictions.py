import contextlib
import io
import json
import re
import unittest
from unittest.mock import patch

import render_strict


def _render_text(data: dict) -> str:
    buf_out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data, ensure_ascii=False))),
        patch("render_strict.pathlib.Path.write_text", return_value=None),
        contextlib.redirect_stdout(buf_out),
    ):
        render_strict.main()
    return buf_out.getvalue()


class TestRenderStrictUXContradictions(unittest.TestCase):
    def test_wait_confirm_never_prints_logical_entry_from_current(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "aggressive",
            "side": "long",
            "entry_mode": "wait_confirm",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"aggressive": {"enabled": True}},
            # near current + correct side would previously trigger the "логичен вход от текущей" line
            "entry_price_aggressive": 99.9,
            "sl_by_mode": {"aggressive": 98.0},
            "tp_by_mode": {"aggressive": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"aggressive": 1.2},
            "exit_plan_by_mode": {"aggressive": "plan"},
        }

        out = _render_text(d)
        self.assertIn("⏳ Вход: wait_confirm", out)
        self.assertNotIn("Логичен вход от текущей", out)

    def test_m15_text_never_claims_uptrend_when_bearish_evidence(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "ADA/USDT",
            "price": 1.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {
                "m5": "—",
                "m15": "устойчивый ап-тренд / бычий каркас, цена выше EMA20",
                "h1": "—",
                "h4": "—",
                "d1": "—",
            },
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 0.99,
            "sl_by_mode": {"neutral": 0.98},
            "tp_by_mode": {"neutral": {"tvh1": 1.01, "tvh2": 1.02}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
            "price_vs_ema20_m15": "below",
            "ema_fan_m15_state": "bear",
            "price_vs_ema20_h1": "below",
            "ema_fan_h1_state": "bear",
        }

        out = _render_text(d)
        self.assertNotRegex(out, re.compile(r"(?i)ап[\s-]*тренд"))
        self.assertNotRegex(out, re.compile(r"(?i)выше\s+ema\s*20"))

    def test_rr_prints_by_mode_when_present(self) -> None:
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
            "entry_price_neutral": 99.0,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr": 9.9,  # should not be shown when rr_by_mode[neutral] is present
            "rr_by_mode": {"neutral": 1.7},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = _render_text(d)
        self.assertIn("RR: 1:1.7", out)


if __name__ == "__main__":
    unittest.main()

