import contextlib
import io
import json
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


class TestRenderAggressiveCountertrendCaution(unittest.TestCase):
    def test_renders_countertrend_wait_confirm_caution_line(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "aggressive",
            "side": "short",
            "entry_mode": "wait_confirm",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"aggressive": {"enabled": True}},
            "entry_price_aggressive": 100.0,
            "sl_by_mode": {"aggressive": 105.0},
            "tp_by_mode": {"aggressive": {"tvh1": 95.0, "tvh2": 90.0}},
            "rr_by_mode": {"aggressive": 1.2},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "warnings": ["aggressive_countertrend_no_evidence_wait_confirm"],
        }

        out = _render_text(d)
        self.assertIn(
            "⚠️ Контртренд против сильного H1 — вход только после подтверждения (wait_confirm).",
            out,
        )


if __name__ == "__main__":
    unittest.main()

