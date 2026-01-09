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


class TestNoTradeExplanation(unittest.TestCase):
    def test_no_trade_is_multiline_and_has_decision_path_elements(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTCUSDT",
            "price": 100.0,
            "mode": "neutral",
            "no_trade": True,
            "no_trade_reasons": ["недостаточный RR для входа", "time_window"],
            "no_trade_hint": "недостаточный RR + риск-окно",
            "warnings": ["mode_fallback: aggressive->neutral", "impulse_no_exhale"],
            "entries": {
                "aggressive": {"enabled": False, "disabled_by": ["impulse_no_exhale"]},
                "neutral": {"enabled": True},
                "conservative": {"enabled": True},
            },
        }

        out = _render_text(d)

        # Multi-line explanation, not a single sentence.
        self.assertGreaterEqual(len([ln for ln in out.splitlines() if ln.strip()]), 8)
        self.assertIn("📌 Сигнал не выдан", out)

        # Explicit mention of rejected aggressive mode + explicit downgrade mention.
        self.assertIn("Агрессивный", out)
        self.assertIn("downgrade", out.lower())

        # Must contain at least one concrete condition for future entry.
        self.assertIn("Что должно измениться", out)
        self.assertRegex(out, r"–\s+")
        self.assertIn("EMA20", out)

    def test_neutral_no_trade_renders_explicit_aggressive_option_line(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "XRP/USDT",
            "price": 2.2,
            "mode": "neutral",
            "no_trade": True,
            "no_trade_reasons": ["neutral_too_close_risky"],
            "no_trade_hint": "слишком близко к текущей цене",
            "side": "short",
            "entries": {"neutral": {"enabled": True}},
            "aggressive_option": {"entry_price": 2.13},
        }

        out = _render_text(d)

        self.assertIn("⚡ Aggressive option:", out)
        self.assertIn("XRP/USDT", out)
        self.assertIn("SHORT", out)
        self.assertIn("entry 2.1300", out)


if __name__ == "__main__":
    unittest.main()
