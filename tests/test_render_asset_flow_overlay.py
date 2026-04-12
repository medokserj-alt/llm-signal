import contextlib
import io
import json
import unittest
from unittest.mock import patch

import render_strict


class TestRenderAssetFlowOverlay(unittest.TestCase):
    def test_render_includes_flow_overlay_execution_line(self) -> None:
        data = {
            "time_msk": "12.04.2026, 12:00",
            "symbol": "SOL/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "short",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 101.0,
            "sl_by_mode": {"neutral": 103.0},
            "tp_by_mode": {"neutral": {"tvh1": 98.0, "tvh2": 96.0}},
            "rr_by_mode": {"neutral": 1.5},
            "exit_plan_by_mode": {"neutral": "plan"},
            "flow_overlay": {
                "display_lines": ["⚠️ Flow opposes fresh continuation shorts; squeeze risk is elevated."]
            },
        }

        buf_out = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(data, ensure_ascii=False))),
            patch("render_strict.pathlib.Path.write_text", return_value=None),
            contextlib.redirect_stdout(buf_out),
        ):
            render_strict.main()

        out = buf_out.getvalue()
        self.assertIn("Flow opposes fresh continuation shorts; squeeze risk is elevated.", out)


if __name__ == "__main__":
    unittest.main()
