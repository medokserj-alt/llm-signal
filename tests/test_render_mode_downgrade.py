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


class TestRenderModeDowngrade(unittest.TestCase):
    def test_renders_mode_downgrade_with_reason_when_trade_issued(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTCUSDT",
            "price": 100.0,
            "mode": "neutral",
            "requested_mode": "aggressive",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "warnings": [
                "mode_fallback: aggressive->neutral",
                "mode_disabled_by: aggressive: ema_guard_between",
            ],
            "decision_path": [
                {"mode": "aggressive", "result": "rejected", "reason": "ema_guard_between"},
                {"mode": "neutral", "result": "accepted"},
            ],
            "entries": {
                "aggressive": {"enabled": False, "disabled_by": ["ema_guard_between"]},
                "neutral": {"enabled": True},
            },
            "entry_price_neutral": 101.0,
            "sl_by_mode": {"neutral": 99.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 103.0}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
        }
        out = _render_text(d)
        self.assertIn("2️⃣ Сетап", out)
        self.assertIn("🔁 Downgrade: aggressive → neutral", out)
        self.assertIn("Причина:", out)
        self.assertIn("между EMA20", out)
        self.assertLess(out.index("Режим:"), out.index("🔁 Downgrade:"))
        self.assertLess(out.index("🔁 Downgrade:"), out.index("Направление:"))


if __name__ == "__main__":
    unittest.main()

