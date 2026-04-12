import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json


class TestFlowDerivativesMidIntegration(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "timestamp_utc": "2026-04-11T12:00:00Z",
            "mode": "observe_only",
            "market_context": {
                "bias": "bullish",
                "confidence": 0.76,
                "crowding_state": "mixed",
                "exchange_pressure": "medium",
                "stablecoin_support": "high",
                "unlock_pressure": "medium",
                "summary": "Flow is supportive, but MID should still treat it as an overlay rather than a standalone trend call.",
            },
            "asset_contexts": {
                "APT": {
                    "asset": "APT",
                    "flow_derivatives_context": {
                        "bias": "bearish",
                    },
                }
            },
            "raw_metrics": {"APT": {"derivatives": {"open_interest": 1}}},
        }

    def test_mid_reads_only_market_context_from_v2_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context_v2.json"
            path.write_text(json.dumps(self._snapshot(), ensure_ascii=False), encoding="utf-8")

            out = get_signal_json.read_mid_aia_flow_derivatives_context(path)

        self.assertEqual(out["market_context"]["bias"], "bullish")
        self.assertEqual(out["market_context"]["stablecoin_support"], "high")
        self.assertNotIn("asset_contexts", out)
        self.assertNotIn("raw_metrics", out)

    def test_mid_prompt_block_keeps_flow_advisory_for_regime_not_override(self) -> None:
        block = get_signal_json.build_flow_derivatives_prompt_block(
            self._snapshot(),
            analysis_profile="mid",
        )

        self.assertIn("FLOW / DERIVATIVES CONTEXT", block)
        self.assertIn("вне MID", block)
        self.assertIn("3–7 day context", block)
        self.assertIn("НЕ заменяет собственную оценку MID", block)
        self.assertIn("НЕ должен сам по себе переворачивать weekly bias или direction", block)
        self.assertIn("less clean downside или squeeze risk", block)
        self.assertIn("two-sided regime", block)
        self.assertIn("\"market_context\"", block)
        self.assertNotIn("\"asset_contexts\"", block)

    def test_mid_render_flow_derivatives_context_section_outputs_dedicated_block(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
        )

        self.assertIn("Flow / Derivatives Context", rendered)
        self.assertIn("Market bias: bullish (confidence 0.76)", rendered)
        self.assertIn("Crowding: mixed", rendered)
        self.assertIn("Exchange pressure: medium", rendered)
        self.assertIn("Stablecoin support: high", rendered)
        self.assertIn("Unlock pressure: medium", rendered)
        self.assertIn("Summary: Flow is supportive, but MID should still treat it as an overlay", rendered)

    def test_mid_missing_or_invalid_flow_file_omits_block_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.json"
            invalid = Path(td) / "invalid.json"
            invalid.write_text(
                json.dumps({"market_context": {"bias": "sideways", "confidence": "high"}}, ensure_ascii=False),
                encoding="utf-8",
            )

            missing_out = get_signal_json.read_mid_aia_flow_derivatives_context(missing)
            invalid_out = get_signal_json.read_mid_aia_flow_derivatives_context(invalid)
            missing_rendered = get_signal_json.render_flow_derivatives_context_section(missing_out)
            invalid_rendered = get_signal_json.render_flow_derivatives_context_section(invalid_out)

        self.assertEqual(missing_out, {})
        self.assertEqual(invalid_out, {})
        self.assertEqual(missing_rendered, "")
        self.assertEqual(invalid_rendered, "")
