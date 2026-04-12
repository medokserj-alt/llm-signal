import copy
import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json


class TestFlowDerivativesDayIntegration(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "timestamp_utc": "2026-04-11T12:00:00Z",
            "mode": "observe_only",
            "market_context": {
                "bias": "bearish",
                "confidence": 0.78,
                "crowding_state": "mixed",
                "exchange_pressure": "high",
                "stablecoin_support": "low",
                "unlock_pressure": "medium",
                "drivers": [
                    "price is weak while flow still shows residual support",
                    "exchange inflows point to elevated sell pressure",
                    "stablecoin support is patchy",
                ],
                "summary": "Bearish flow overlay with mixed positioning: underlying support exists, but the price regime is still fragile.",
            },
            "asset_contexts": {
                "APT": {
                    "asset": "APT",
                    "flow_derivatives_context": {
                        "bias": "bullish",
                    },
                }
            },
            "raw_metrics": {"APT": {"derivatives": {"open_interest": 1}}},
        }

    def test_day_prompt_block_keeps_flow_advisory_not_direction_override(self) -> None:
        block = get_signal_json.build_flow_derivatives_prompt_block(self._snapshot())

        self.assertIn("FLOW / DERIVATIVES CONTEXT", block)
        self.assertIn("EXTERNAL, ADVISORY ONLY", block)
        self.assertIn("не должен пересчитываться внутри DAY", block)
        self.assertIn("НЕ заменяет собственную оценку DAY", block)
        self.assertIn("НЕ должен сам по себе переворачивать direction", block)
        self.assertIn("\"market_context\"", block)
        self.assertNotIn("\"asset_contexts\"", block)

    def test_read_aia_flow_derivatives_context_reads_only_market_context_from_v2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context_v2.json"
            path.write_text(json.dumps(self._snapshot(), ensure_ascii=False), encoding="utf-8")

            out = get_signal_json.read_aia_flow_derivatives_context(path)

        self.assertEqual(out["market_context"]["bias"], "bearish")
        self.assertEqual(out["market_context"]["crowding_state"], "mixed")
        self.assertNotIn("asset_contexts", out)
        self.assertNotIn("raw_metrics", out)

    def test_missing_or_invalid_flow_file_omits_block_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.json"
            out = get_signal_json.read_aia_flow_derivatives_context(missing)
            rendered = get_signal_json.render_flow_derivatives_context_section(out)

        self.assertEqual(out, {})
        self.assertEqual(rendered, "")

    def test_render_flow_derivatives_context_section_outputs_dedicated_day_block(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
        )

        self.assertIn("Flow / Derivatives Context", rendered)
        self.assertIn("Market bias: bearish (confidence 0.78)", rendered)
        self.assertIn("Crowding: mixed", rendered)
        self.assertIn("Exchange pressure: high", rendered)
        self.assertIn("Stablecoin support: low", rendered)
        self.assertIn("Unlock pressure: medium", rendered)
        self.assertIn("Summary: Bearish flow overlay with mixed positioning", rendered)

    def test_render_flow_derivatives_context_section_does_not_mutate_signal_fields(self) -> None:
        signal = {
            "symbol": "APT/USDT",
            "direction": "long",
            "market_context": "Risk-off price regime with weak breadth.",
            "entry_range": {"min": 7.1, "max": 7.3},
            "sl_by_mode": {"neutral": 6.9},
            "tp_by_mode": {"neutral": {"tvh1": 7.8, "tvh2": 8.2}},
        }
        original = copy.deepcopy(signal)

        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload=signal,
        )

        self.assertIn("Flow / Derivatives Context", rendered)
        self.assertEqual(signal, original)
