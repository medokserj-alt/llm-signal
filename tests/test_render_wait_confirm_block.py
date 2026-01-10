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


class TestRenderWaitConfirmBlock(unittest.TestCase):
    def test_wait_confirm_renders_actionable_confirmation_block(self) -> None:
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
            "entry_price_aggressive": 99.5,
            "sl_by_mode": {"aggressive": 98.0},
            "tp_by_mode": {"aggressive": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"aggressive": 1.2},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "warnings": ["aggressive_direction_conflict_with_ema_wait_confirm"],
        }

        out = _render_text(d)
        self.assertIn("⏳ Вход только после подтверждения:", out)
        self.assertIn("⚠️ Направление против EMA-структуры — подтверждение обязательно.", out)
        self.assertIn("Отмена:", out)

    def test_neutral_wait_confirm_includes_short_reason_line(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "entry_mode": "wait_confirm",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 99.5,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
            "warnings": ["neutral_wait_confirm_due_to_rr"],
        }

        out = _render_text(d)
        self.assertIn("⏳ Neutral ждёт подтверждение: нужен глубже вход/лучше RR.", out)


if __name__ == "__main__":
    unittest.main()
