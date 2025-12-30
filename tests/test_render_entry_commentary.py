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
        "entry_price_neutral": 100.0,
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


class TestRenderEntryCommentary(unittest.TestCase):
    def test_long_entry_range_around_current_marks_aggressive_from_current(self) -> None:
        d = _base_signal_data()
        d["side"] = "long"
        d["entry_range"] = {"min": 99.95, "max": 100.05}
        out = _render_text(d)
        self.assertIn("**✅ Логичен вход от текущей / вблизи текущей (агрессивно).**", out)
        self.assertIn("Вход:", out)
        self.assertNotIn("ℹ️ Вход через лимит в зоне", out)
        self.assertNotIn("Зона активации:", out)
        self.assertNotIn("Зона входа:", out)

    def test_long_entry_range_above_current_warns_and_uses_activation_zone(self) -> None:
        d = _base_signal_data()
        d["side"] = "long"
        d["entry_range"] = {"min": 101.0, "max": 102.0}
        d["entry_price_neutral"] = 101.5
        out = _render_text(d)
        self.assertIn("⚠️ Вход расположен *выше текущей цены* (для LONG)", out)
        self.assertIn("Зона активации:", out)
        self.assertNotIn("ℹ️ Вход через лимит в зоне", out)
        self.assertNotIn("Вход: ", out)

    def test_missing_entry_price_falls_back_to_entry_zone(self) -> None:
        d = _base_signal_data()
        d["entry_price_neutral"] = None
        d["entry_range"] = {"min": 99.0, "max": 100.0}
        out = _render_text(d)
        self.assertIn("2️⃣ Сетап", out)
        self.assertIn("Зона входа:", out)
        self.assertNotIn("Вход: ", out)
        self.assertNotIn("Зона активации:", out)
        self.assertNotIn("ℹ️ Вход через лимит в зоне", out)


if __name__ == "__main__":
    unittest.main()
