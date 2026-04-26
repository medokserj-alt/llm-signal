import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import get_signal_json


class TestFlowDerivativesMidIntegration(unittest.TestCase):
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
                "bias": "bullish",
                "confidence": 0.76,
                "crowding_state": "mixed",
                "exchange_pressure": "unavailable",
                "stablecoin_support": "unavailable",
                "unlock_pressure": "unavailable",
                "summary": "Flow is supportive, but MID should still treat it as an overlay rather than a standalone trend call.",
                "flow_derivatives_modifiers": {
                    "directional_bias": "bullish",
                    "positioning_risk": "medium",
                    "squeeze_risk": "medium",
                    "chase_risk": "medium",
                    "confirmation_required": True,
                    "reason_codes": ["price_up_oi_down", "deleveraging", "no_live_exchange_flow"],
                },
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
        self.assertEqual(out["market_context"]["stablecoin_support"], "unavailable")
        self.assertEqual(out["coverage"]["exchange_flows"]["source"], "unavailable")
        self.assertEqual(out["data_source"], "fallback")
        self.assertEqual(out["input_coverage"]["assets_with_fallback_data"], 5)
        self.assertIn("freshness_minutes", out)
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
        self.assertIn("derivatives-only signal, NOT full liquidity-flow confirmation", block)
        self.assertIn("less clean downside или squeeze risk", block)
        self.assertIn("two-sided regime", block)
        self.assertIn("high/severe geopolitical regime", block)
        self.assertIn("downside-shock framing", block)
        self.assertIn("\"market_context\"", block)
        self.assertNotIn("\"asset_contexts\"", block)

    def test_mid_render_flow_derivatives_context_section_outputs_compact_block(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
            detail_level="compact",
        )

        self.assertIn("Flow / Derivatives", rendered)
        self.assertIn("Сигнал: bullish, confidence 0.76, source=fallback, freshness=4.6m", rendered)
        self.assertIn(
            "Покрытие: derivatives live 5/5; exchange unavailable, stablecoin unavailable, tokenomics unavailable",
            rendered,
        )
        self.assertIn(
            "Вывод: positioning=bullish, squeeze_risk=medium, chase_risk=medium, confirmation_required=yes",
            rendered,
        )
        self.assertIn(
            "Примечание: Доступен только derivatives-сигнал; exchange/stablecoin/tokenomics недоступны, поэтому это не полное подтверждение liquidity-flow.",
            rendered,
        )
        self.assertNotIn("Диагностика:", rendered)
        self.assertNotIn("Коды причин:", rendered)
        self.assertNotIn("Покрытие входов:", rendered)

    def test_mid_overview_places_flow_block_before_asset_breakdown_tail(self) -> None:
        flow_block = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
            detail_level="compact",
        )

        sections = get_signal_json._compose_overview_sections(
            [
                "Pool mode: mixed rotation with selective longs.",
                "APT: relative strength improving, but overhead supply remains.",
                "BNB: range trade unless breakout confirms.",
            ],
            analysis_profile="mid",
            flow_derivatives_section=flow_block,
        )

        self.assertEqual(sections[0], "Pool mode: mixed rotation with selective longs.")
        self.assertEqual(sections[1], flow_block)
        self.assertIn("APT: relative strength improving", sections[2])
        self.assertIn("BNB: range trade unless breakout confirms.", sections[2])

    def test_mid_flow_block_stays_above_calendar_section_in_render_order(self) -> None:
        flow_block = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
            detail_level="compact",
        )
        overview_sections = get_signal_json._compose_overview_sections(
            [
                "📰 MID • 12:00\n1️⃣ Среднесрочный режим 3–7 дней\nRisk-on but selective.",
                "4️⃣ Карта активов\nStrong: BTC, SOL.",
            ],
            analysis_profile="mid",
            flow_derivatives_section=flow_block,
        )
        calendar_block = get_signal_json.render_calendar_section(
            [
                {
                    "date_msk": "12.04.2026",
                    "time_msk": "12.04.2026, 15:30",
                    "event": "US CPI",
                    "category": "macro",
                    "impact": "high",
                    "note": "Volatility reset risk.",
                }
            ]
        )
        final_render = "\n\n".join(overview_sections + [calendar_block])

        self.assertLess(final_render.index("Flow / Derivatives"), final_render.index("🗓 Ключевые события периода"))

    def test_debug_render_flow_derivatives_context_section_keeps_full_diagnostics(self) -> None:
        rendered = get_signal_json.render_flow_derivatives_context_section(
            self._snapshot(),
            signal_payload={"symbol": "APT/USDT"},
            detail_level="debug",
        )

        self.assertIn("Диагностика: observe_only_deterministic_fallback_no_live_collectors_configured", rendered)
        self.assertIn("Коды причин: price_up_oi_down, deleveraging, no_live_exchange_flow", rendered)
        self.assertIn("Вывод: Flow is supportive, but MID should still treat it as an overlay", rendered)

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

    def test_mid_stale_snapshot_keeps_visibility_but_is_marked_ignored(self) -> None:
        snapshot = self._snapshot()
        snapshot["generated_at"] = "2026-04-10T12:00:00Z"
        snapshot["timestamp_utc"] = "2026-04-10T12:00:00Z"

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "flow_derivatives_context_v2.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            out = get_signal_json.read_mid_aia_flow_derivatives_context(path)

        rendered = get_signal_json.render_flow_derivatives_context_section(out, detail_level="compact")
        prompt_block = get_signal_json.build_flow_derivatives_prompt_block(out, analysis_profile="mid")

        self.assertEqual(out.get("status"), "stale")
        self.assertIn("Статус: stale, не используется в текущем решении", rendered)
        self.assertIn("\"status\": \"stale\"", prompt_block)
        self.assertNotIn("\"market_context\"", prompt_block)
