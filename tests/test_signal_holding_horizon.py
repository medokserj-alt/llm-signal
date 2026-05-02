import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import get_signal_json
import render_strict


def _aggressive_signal() -> dict:
    return {
        "no_trade": False,
        "mode": "aggressive",
        "holding_horizon": "short_swing",
        "symbol": "ETH/USDT",
        "price": 2500.0,
        "side": "long",
        "entry_mode": "wait_confirm",
        "why_asset": "Ближайшие уровни позволяют задать понятный SL и собрать RR для горизонта 3–7 дней.",
        "technical_rationale": "Сетап остаётся аккуратным, потому что риск формализуется для горизонта 3–7 дней.",
        "entries": {
            "aggressive": {"enabled": True, "range": {"min": 2495.0, "max": 2502.0}},
            "neutral": {"enabled": True, "range": {"min": 2475.0, "max": 2485.0}},
            "conservative": {"enabled": True, "range": {"min": 2450.0, "max": 2460.0}},
        },
        "entry_range": {"min": 2495.0, "max": 2502.0},
        "entry_price_aggressive": 2499.0,
        "sl_by_mode": {"aggressive": 2474.0},
        "tp_by_mode": {"aggressive": {"tvh1": 2528.0, "tvh2": 2554.0, "tvh3": 2578.0}},
        "rr_by_mode": {"aggressive": 1.16},
        "exit_plan_by_mode": {"aggressive": "TP1 partial; TP2; TP3 optional"},
        "price_vs_ema20_h1": "above",
        "price_vs_ema20_m15": "above",
        "ema_fan_h1_state": "bull",
        "ema_fan_m15_state": "bull",
        "warnings": [],
    }


def _neutral_signal() -> dict:
    data = _aggressive_signal()
    data["mode"] = "neutral"
    data["entry_mode"] = "wait_confirm"
    data["entry_price_neutral"] = 2480.0
    data["sl_by_mode"]["neutral"] = 2468.0
    data["tp_by_mode"]["neutral"] = {"tvh1": 2515.0, "tvh2": 2540.0}
    data["rr_by_mode"]["neutral"] = 1.5
    data["exit_plan_by_mode"]["neutral"] = "TP1 partial; TP2 trail"
    return data


def _render_text(data: dict) -> str:
    buf_out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data))),
        patch("sys.argv", ["render_strict.py"]),
        patch("render_strict.pathlib.Path.write_text", return_value=None),
        contextlib.redirect_stdout(buf_out),
    ):
        render_strict.main()
    return buf_out.getvalue()


class TestSignalHoldingHorizon(unittest.TestCase):
    def test_aggressive_signal_uses_intraday_horizon_and_rewrites_mid_wording(self) -> None:
        out = get_signal_json.validate_active_mode_setup(_aggressive_signal())

        self.assertEqual(out.get("holding_horizon"), "intraday_to_1_2d")
        self.assertEqual(out.get("holding_horizon_label"), "intraday / 1–2 дня")
        self.assertNotIn("3–7 дней", str(out.get("why_asset") or ""))
        self.assertIn("intraday / 1–2 дня", str(out.get("why_asset") or ""))
        self.assertNotIn("3–7 дней", str(out.get("technical_rationale") or ""))

    def test_render_outputs_tactical_horizon_for_aggressive_signal(self) -> None:
        rendered = _render_text(get_signal_json.validate_active_mode_setup(_aggressive_signal()))

        self.assertIn("Горизонт: intraday / 1–2 дня", rendered)
        self.assertNotIn("Горизонт: 1–3 дня / short swing", rendered)
        self.assertNotIn("RR для горизонта 3–7 дней", rendered)

    def test_render_overrides_raw_aggressive_short_swing_horizon(self) -> None:
        rendered = _render_text(_aggressive_signal())

        self.assertIn("Горизонт: intraday / 1–2 дня", rendered)
        self.assertNotIn("Горизонт: 1–3 дня / short swing", rendered)

    def test_neutral_signal_uses_short_swing_horizon(self) -> None:
        out = get_signal_json.validate_active_mode_setup(_neutral_signal())
        rendered = _render_text(out)

        self.assertEqual(out.get("holding_horizon"), "short_swing")
        self.assertEqual(out.get("holding_horizon_label"), "1–3 дня / short swing")
        self.assertIn("Горизонт: 1–3 дня / short swing", rendered)

    def test_mid_and_day_prompts_keep_their_view_horizon_contracts(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        prompt_mid = (repo_root / "prompt_mid.txt").read_text(encoding="utf-8")
        prompt_day = (repo_root / "prompt_day.txt").read_text(encoding="utf-8")

        self.assertIn("горизонта 3–7 дней", prompt_mid)
        self.assertIn("DAY = tactical current-day view", prompt_day)


if __name__ == "__main__":
    unittest.main()
