import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import get_signal_json


class TestFlowDerivativesDayIntegration(unittest.TestCase):
    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _snapshot(self) -> dict:
        now_iso = self._now_iso()
        return {
            "timestamp_utc": now_iso,
            "generated_at": now_iso,
            "data_source": "fallback",
            "freshness_minutes": 4.6,
            "input_coverage": {"assets_total": 5, "assets_with_live_data": 0, "assets_with_fallback_data": 5},
            "coverage": {
                "derivatives": {"source": "live", "assets_total": 5, "assets_live": 5, "assets_fallback": 0},
                "exchange_flows": {"source": "unavailable"},
                "stablecoin_flows": {"source": "unavailable"},
                "tokenomics": {"source": "unavailable"},
            },
            "diagnostics": {"reason": "observe_only_deterministic_fallback_no_live_collectors_configured"},
            "mode": "observe_only",
            "market_context": {
                "bias": "bearish",
                "confidence": 0.78,
                "crowding_state": "mixed",
                "exchange_pressure": "unavailable",
                "stablecoin_support": "unavailable",
                "unlock_pressure": "unavailable",
                "drivers": [
                    "price is weak while flow still shows residual support",
                    "exchange inflows point to elevated sell pressure",
                    "stablecoin support is patchy",
                ],
                "summary": "Bearish flow overlay with mixed positioning: underlying support exists, but the price regime is still fragile.",
                "flow_derivatives_modifiers": {
                    "directional_bias": "bearish",
                    "positioning_risk": "medium",
                    "squeeze_risk": "medium",
                    "chase_risk": "high",
                    "confirmation_required": True,
                    "reason_codes": ["oi_rising", "funding_neutral", "no_live_exchange_flow"],
                },
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
        self.assertIn("derivatives-only signal, NOT full liquidity-flow confirmation", block)
        self.assertIn("high/severe geopolitical regime", block)
        self.assertIn("buy-the-dip framing", block)
        self.assertIn("\"market_context\"", block)
        self.assertNotIn("\"asset_contexts\"", block)

    def test_read_aia_flow_derivatives_context_reads_only_market_context_from_v2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context_v2.json"
            path.write_text(json.dumps(self._snapshot(), ensure_ascii=False), encoding="utf-8")

            out = get_signal_json.read_aia_flow_derivatives_context(path)

        self.assertEqual(out["market_context"]["bias"], "bearish")
        self.assertEqual(out["market_context"]["crowding_state"], "mixed")
        self.assertEqual(out["data_source"], "fallback")
        self.assertEqual(out["input_coverage"]["assets_with_live_data"], 0)
        self.assertEqual(out["coverage"]["exchange_flows"]["source"], "unavailable")
        self.assertIn("freshness_minutes", out)
        self.assertNotIn("asset_contexts", out)
        self.assertNotIn("raw_metrics", out)

    def test_missing_or_invalid_flow_file_omits_block_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.json"
            out = get_signal_json.read_aia_flow_derivatives_context(missing)
            rendered = get_signal_json.render_flow_derivatives_context_section(out)

        self.assertEqual(out, {})
        self.assertEqual(rendered, "")

    def test_render_flow_derivatives_context_section_outputs_compact_day_block(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
            detail_level="day_compact",
        )

        self.assertIn("Flow / Derivatives", rendered)
        self.assertIn("Сигнал: bearish, confidence 0.78, source=fallback, freshness=4.6m", rendered)
        self.assertIn(
            "Покрытие: derivatives live 5/5; exchange unavailable, stablecoin unavailable, tokenomics unavailable",
            rendered,
        )
        self.assertIn("Вывод: positioning=bearish, chase_risk=high, confirmation_required=yes", rendered)
        self.assertNotIn("Диагностика:", rendered)
        self.assertNotIn("Коды причин:", rendered)
        self.assertNotIn("Покрытие входов:", rendered)
        self.assertNotIn("Примечание:", rendered)
        self.assertNotIn("Summary:", rendered)

    def test_day_compact_render_includes_stablecoin_layer_and_support_when_live(self) -> None:
        snapshot = self._snapshot()
        snapshot["coverage"]["stablecoin_flows"] = {"source": "live"}
        snapshot["market_context"]["stablecoin_support"] = "medium"

        rendered = get_signal_json.render_flow_derivatives_context_section(
            snapshot,
            signal_payload={"symbol": "APT/USDT"},
            detail_level="day_compact",
        )

        self.assertIn(
            "Покрытие: derivatives live 5/5; stablecoin live; exchange/tokenomics unavailable",
            rendered,
        )
        self.assertIn("stablecoin_support=medium", rendered)

    def test_debug_detail_level_keeps_detailed_day_render(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
            detail_level="debug",
        )

        self.assertIn("Flow / Derivatives", rendered)
        self.assertIn("Диагностика: observe_only_deterministic_fallback_no_live_collectors_configured", rendered)
        self.assertIn("Коды причин: oi_rising, funding_neutral, no_live_exchange_flow", rendered)
        self.assertIn("Вывод: Bearish flow overlay with mixed positioning", rendered)
        self.assertIn("Примечание: Доступен только derivatives-сигнал", rendered)

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

        self.assertIn("Flow / Derivatives", rendered)
        self.assertEqual(signal, original)

    def test_stale_day_flow_snapshot_is_marked_ignored_in_render_and_prompt(self) -> None:
        snapshot = self._snapshot()
        snapshot["generated_at"] = "2026-04-10T12:00:00Z"
        snapshot["timestamp_utc"] = "2026-04-10T12:00:00Z"

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context_v2.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            out = get_signal_json.read_aia_flow_derivatives_context(path)

        rendered = get_signal_json.render_flow_derivatives_context_section(out)
        prompt_block = get_signal_json.build_flow_derivatives_prompt_block(out)

        self.assertEqual(out.get("status"), "stale")
        self.assertNotIn("market_context", out)
        self.assertIn("Статус: stale, не используется в текущем решении", rendered)
        self.assertIn("Последний bias: bearish", rendered)
        self.assertIn("\"status\": \"stale\"", prompt_block)
        self.assertIn("\"ignored_for_current_decision\": true", prompt_block)
        self.assertNotIn("\"market_context\"", prompt_block)
