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


def _base_payload(*, warnings: list[str]) -> dict:
    return {
        "no_trade": False,
        "mode": "neutral",
        "symbol": "BTC/USDT",
        "price": 100.0,
        "side": "long",
        "entries": {"neutral": {"enabled": True}},
        "entry_range": {"min": 97.5, "max": 98.5},
        "entry_price_neutral": 98.0,
        "sl_by_mode": {"neutral": 95.0},
        "tp_by_mode": {"neutral": {"tvh1": 105.0, "tvh2": 110.0}},
        "rr_by_mode": {"neutral": 1.5},
        "exit_plan_by_mode": {"neutral": "plan"},
        # Trigger the stabilization gate (wrong-side EMA20 on both TFs for LONG),
        # but avoid the strict counter-trend gate by keeping H1 fan != "bear".
        "price_vs_ema20_m15": "below",
        "price_vs_ema20_h1": "below",
        "ema_fan_m15_state": "bear",
        "ema_fan_h1_state": "bull",
        "warnings": warnings,
    }


class TestNeutralStabilizationGate(unittest.TestCase):
    def test_neutral_rejected_without_stabilization_evidence(self) -> None:
        d = _base_payload(warnings=["impulse_no_exhale"])
        out = get_signal_json.validate_active_mode_setup(d)

        self.assertTrue(bool(out.get("no_trade")))
        self.assertIn("neutral_continuation_unstable_forbidden", out.get("no_trade_reasons") or [])
        self.assertIsInstance(out.get("aggressive_option"), dict)

        rendered = _render_text(out)
        self.assertIn("📌 Сигнал не выдан", rendered)
        self.assertIn("Aggressive option", rendered)

    def test_neutral_allowed_with_stabilization_evidence(self) -> None:
        d = _base_payload(warnings=[])
        # Provide stabilization evidence via M15 EMA-fan (avoid flush structural fallback).
        d["ema_fan_m15_state"] = "bull"
        out = get_signal_json.validate_active_mode_setup(d)

        self.assertFalse(bool(out.get("no_trade")))
        self.assertNotIn("neutral_continuation_unstable_forbidden", out.get("no_trade_reasons") or [])


if __name__ == "__main__":
    unittest.main()
