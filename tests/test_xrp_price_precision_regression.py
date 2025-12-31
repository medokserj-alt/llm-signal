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
        patch("sys.stdin", io.StringIO(json.dumps(data, ensure_ascii=False))),
        patch("render_strict.pathlib.Path.write_text", return_value=None),
        contextlib.redirect_stdout(buf_out),
    ):
        render_strict.main()
    return buf_out.getvalue()


class TestXrpPricePrecisionRegression(unittest.TestCase):
    def test_llm_coarse_tvh1_is_rendered_with_4_decimals_for_xrp(self) -> None:
        # Regression: LLM can return coarse decimals like 1.85 for XRP TVH/TP levels.
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "XRP/USDT",
            "price": 1.87,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 1.84,
            "sl_by_mode": {"neutral": 1.83},
            "tp_by_mode": {"neutral": {"tvh1": 1.85, "tvh2": 1.86}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        get_signal_json.quantize_price_levels_to_symbol_precision(d)

        out = _render_text(d)
        self.assertIn("Вход: 1.8400", out)
        self.assertIn("SL: 1.8300", out)
        self.assertIn("TP1: 1.8500", out)
        self.assertIn("TP2: 1.8600", out)


if __name__ == "__main__":
    unittest.main()

