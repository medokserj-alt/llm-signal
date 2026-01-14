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


class TestRenderNoEntryRanges(unittest.TestCase):
    def test_neutral_with_aggressive_option_renders_single_prices_only(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True, "range": {"min": 99.6, "max": 99.8}}},
            "entry_range": {"min": 99.6, "max": 99.8},
            "entry_price_neutral": 99.7,
            "entry_price_aggressive": 99.9,
            "sl_by_mode": {"neutral": 98.5},
            "tp_by_mode": {"neutral": {"tvh1": 101.0, "tvh2": 102.0}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
            "aggressive_option": {"entry_price": 99.9},
        }

        out = _render_text(d)
        self.assertIn("Вход: 99.70", out)
        self.assertNotIn("⚡ Возможен агрессивный вход:", out)

        # No user-facing entry ranges like "a–b".
        self.assertIsNone(re.search(r"\\d[\\d.]*\\s*–\\s*\\d", out))


if __name__ == "__main__":
    unittest.main()
