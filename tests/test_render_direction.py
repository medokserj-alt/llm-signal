import contextlib
import io
import json
import unittest
from unittest.mock import patch

import render_strict


def _base_signal_data() -> dict:
    return {
        "time_msk": "01.01.2025, 00:00",
        "symbol": "BTCUSDT",
        "price": 100.0,
        "mode": "neutral",
        "why_asset": "test",
        "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
        "news_context": [],
        "entries": {"neutral": {"enabled": True}},
        "entry_price_neutral": 101.0,
        "sl_by_mode": {"neutral": 99.0},
        "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 103.0}},
        "rr_by_mode": {"neutral": 1.2},
        "exit_plan_by_mode": {"neutral": "plan"},
    }


def _render_text(data: dict) -> str:
    buf_out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data))),
        patch("render_strict.pathlib.Path.write_text", return_value=None),
        contextlib.redirect_stdout(buf_out),
    ):
        render_strict.main()
    return buf_out.getvalue()


class TestRenderDirection(unittest.TestCase):
    def test_direction_long_uses_side_precedence(self) -> None:
        d = _base_signal_data()
        d["side"] = "long"
        d["direction"] = "short"
        out = _render_text(d)
        self.assertIn("2️⃣ Сетап", out)
        self.assertIn("Направление: 🟩 LONG", out)
        self.assertLess(out.index("Режим:"), out.index("Направление:"))

    def test_direction_short_falls_back_to_direction(self) -> None:
        d = _base_signal_data()
        d["direction"] = "short"
        out = _render_text(d)
        self.assertIn("Направление: 🟥 SHORT", out)


if __name__ == "__main__":
    unittest.main()

