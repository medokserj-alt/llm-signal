import contextlib
import io
import json
import unittest
from unittest.mock import patch

import render_strict
import no_trade_explain


def _render_text(data: dict) -> str:
    buf_out = io.StringIO()
    with (
        patch("sys.argv", ["render_strict"]),
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

    def test_low_rr_explanation_uses_trader_facing_wording(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 78950.0,
            "mode": "aggressive",
            "no_trade": True,
            "no_trade_reasons": ["недостаточный RR для входа"],
            "no_trade_hint": "недостаточный RR для входа",
        }

        out = _render_text(d)

        self.assertIn("слишком близко к ближайшим целям", out)
        self.assertIn("нужен либо откат к более выгодной зоне входа", out.lower())
        self.assertNotIn("TP-лестниц", out)
        self.assertNotIn("snapshot", out)

    def test_custom_human_no_trade_hint_is_rendered_verbatim(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BTC/USDT",
            "price": 78950.0,
            "mode": "aggressive",
            "no_trade": True,
            "no_trade_reasons": [],
            "no_trade_hint": "цена уже подошла слишком близко к ближайшим сопротивлениям, поэтому первая и вторая цели не дают нормального запаса хода.",
        }

        out = _render_text(d)

        self.assertIn("слишком близко к ближайшим сопротивлениям", out)
        self.assertNotIn("условия входа сейчас не соответствуют требованиям стратегии", out)

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

    def test_downgraded_tp_diagnostic_uses_effective_contract_mode_and_persists_audit(self) -> None:
        payload = {
            "mode": "neutral",
            "requested_mode": "aggressive",
            "warnings": ["mode_fallback: aggressive->neutral"],
            "no_trade": True,
            "no_trade_reasons": [
                "Невозможно собрать валидный TP ladder по режиму aggressive: "
                "доступный сверху уровень структуры только 1.1637, он слишком близко к entry_range."
            ],
            "rr": 0.45,
        }

        no_trade_explain.ensure_decision_path(payload)

        self.assertEqual(payload["requested_mode"], "aggressive")
        self.assertEqual(payload["effective_mode"], "neutral")
        self.assertEqual(payload["contract_mode"], "neutral")
        self.assertEqual(payload["published_mode"], "neutral")
        self.assertEqual(payload["mode_transition_reason"], "mode_fallback: aggressive->neutral")
        self.assertEqual(payload["required_rr"], 1.5)
        self.assertEqual(payload["calculated_rr"], 0.45)
        self.assertFalse(payload["tp_ladder_valid"])
        self.assertEqual(payload["structural_target_available"], 1.1637)
        self.assertIn("по режиму neutral", payload["tp_ladder_failure_reason"])
        self.assertNotIn("по режиму aggressive", payload["decision_path"][1]["reason"])

        out = _render_text(payload)
        self.assertIn("по режиму neutral", out)
        self.assertNotIn("TP ladder по режиму aggressive", out)


if __name__ == "__main__":
    unittest.main()
