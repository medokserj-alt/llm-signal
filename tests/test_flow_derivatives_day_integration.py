import copy
import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json


class TestFlowDerivativesDayIntegration(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "asset": "APT",
            "timestamp_utc": "2026-04-11T12:00:00Z",
            "mode": "observe_only",
            "flow_derivatives_context": {
                "bias": "bearish",
                "confidence": 0.78,
                "crowding_state": "long_crowded",
                "exchange_pressure": "high",
                "stablecoin_support": "low",
                "unlock_pressure": "medium",
                "drivers": [
                    "long crowding remains elevated",
                    "exchange inflows point to high sell pressure",
                    "stablecoin support on exchanges is weak",
                ],
                "summary": "Bearish observe-only context: long crowding remains elevated; exchange inflows point to high sell pressure.",
            },
            "raw_metrics": {},
        }

    def test_day_prompt_block_includes_external_observe_only_contract(self) -> None:
        block = get_signal_json.build_flow_derivatives_prompt_block(self._snapshot())

        self.assertIn("FLOW / DERIVATIVES CONTEXT", block)
        self.assertIn("EXTERNAL, OBSERVE_ONLY", block)
        self.assertIn("не должен пересчитываться внутри DAY", block)
        self.assertIn("\"asset\": \"APT\"", block)

    def test_read_aia_flow_derivatives_context_matches_asset_and_sanitizes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context.json"
            path.write_text(json.dumps(self._snapshot(), ensure_ascii=False), encoding="utf-8")

            out = get_signal_json.read_aia_flow_derivatives_context(path, asset="APT/USDT")

        self.assertEqual(out["asset"], "APT")
        self.assertEqual(out["mode"], "observe_only")
        self.assertEqual(out["flow_derivatives_context"]["bias"], "bearish")

    def test_read_aia_flow_derivatives_context_ignores_asset_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context.json"
            path.write_text(json.dumps(self._snapshot(), ensure_ascii=False), encoding="utf-8")

            out = get_signal_json.read_aia_flow_derivatives_context(path, asset="BTC/USDT")

        self.assertEqual(out, {})

    def test_render_flow_derivatives_context_section_outputs_dedicated_block(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
        )

        self.assertIn("Flow / Derivatives Context", rendered)
        self.assertIn("Asset: APT", rendered)
        self.assertIn("Bias: bearish", rendered)
        self.assertIn("Crowding: long_crowded", rendered)
        self.assertIn("Stablecoin support: low", rendered)

    def test_render_flow_derivatives_context_section_does_not_mutate_signal_fields(self) -> None:
        signal = {
            "symbol": "APT/USDT",
            "direction": "long",
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
