import contextlib
import io
import json
import unittest
from unittest.mock import patch

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


class TestLowPriceMinPrecisionRender(unittest.TestCase):
    def test_sui_prices_render_with_min_4_decimals(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "SUI/USDT",
            "price": 1.8,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 1.83,
            "sl_by_mode": {"neutral": 1.765},
            "tp_by_mode": {"neutral": {"tvh1": 1.86, "tvh2": 1.88}},
            "rr_by_mode": {"neutral": 1.6},
            "exit_plan_by_mode": {"neutral": "plan"},
        }
        out = _render_text(d)
        self.assertIn("💰 Текущая цена: 1.8000", out)
        self.assertIn("Вход: 1.8300", out)
        self.assertIn("SL: 1.7650", out)
        self.assertIn("TP1: 1.8600", out)
        self.assertIn("TP2: 1.8800", out)


if __name__ == "__main__":
    unittest.main()
