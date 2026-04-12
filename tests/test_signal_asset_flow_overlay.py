import copy
import json
import tempfile
import unittest
from pathlib import Path

import get_signal_json


class TestSignalAssetFlowOverlay(unittest.TestCase):
    def _base_signal(self, *, symbol: str = "ETH/USDT", side: str = "long", confidence: str = "Medium") -> dict:
        return {
            "time_msk": "12.04.2026, 12:00",
            "symbol": symbol,
            "price": 100.0,
            "direction": side,
            "side": side,
            "mode": "neutral",
            "entry_mode": "limit",
            "entry_range": {"min": 99.0, "max": 100.0},
            "sl": 96.0,
            "tp1": 103.0,
            "tp2": 106.0,
            "sl_by_mode": {"neutral": 96.0},
            "tp_by_mode": {"neutral": {"tvh1": 103.0, "tvh2": 106.0}},
            "rr_by_mode": {"neutral": 2.0},
            "confidence": confidence,
            "warnings": [],
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "price_vs_ema20_h1": "above" if side == "long" else "below",
            "ema_fan_h1_state": "bull" if side == "long" else "bear",
            "ema_fan_m15_state": "bull" if side == "long" else "bear",
        }

    def _v2_snapshot(self, *, asset_contexts: dict) -> dict:
        return {
            "timestamp_utc": "2026-04-12T09:00:00Z",
            "mode": "observe_only",
            "market_context": {
                "bias": "neutral",
                "confidence": 0.5,
                "crowding_state": "neutral",
                "exchange_pressure": "low",
                "stablecoin_support": "low",
                "unlock_pressure": "low",
                "drivers": [],
                "summary": "unused by signal overlay",
            },
            "asset_contexts": asset_contexts,
            "raw_metrics": {"ETH": {"ignored": True}},
        }

    def _write_snapshot(self, payload: dict) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "flow_derivatives_context_v2.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_read_signal_asset_flow_context_reads_only_selected_asset_context(self) -> None:
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "ETH": {
                        "asset": "ETH",
                        "timestamp_utc": "2026-04-12T09:00:00Z",
                        "mode": "observe_only",
                        "flow_derivatives_context": {
                            "bias": "bullish",
                            "confidence": 0.81,
                            "crowding_state": "neutral",
                            "exchange_pressure": "low",
                            "stablecoin_support": "high",
                            "unlock_pressure": "low",
                            "drivers": ["stablecoin demand remains healthy"],
                            "summary": "Supportive ETH flow.",
                        },
                    },
                    "BTC": {
                        "asset": "BTC",
                        "flow_derivatives_context": {
                            "bias": "bearish",
                            "confidence": 0.65,
                            "crowding_state": "mixed",
                            "exchange_pressure": "high",
                            "stablecoin_support": "low",
                            "unlock_pressure": "medium",
                            "drivers": ["sell pressure is elevated"],
                            "summary": "Supportive for BTC shorts.",
                        },
                    },
                }
            )
        )

        out = get_signal_json.read_signal_asset_flow_context("ETH/USDT", path)

        self.assertEqual(out["asset"], "ETH")
        self.assertEqual(out["flow_derivatives_context"]["bias"], "bullish")
        self.assertEqual(out["flow_derivatives_context"]["stablecoin_support"], "high")
        self.assertNotIn("market_context", out)
        self.assertNotIn("raw_metrics", out)

    def test_long_supportive_bullish_asset_flow_can_improve_confidence_without_mutating_levels(self) -> None:
        d = self._base_signal(side="long", confidence="Medium")
        before = copy.deepcopy(
            {
                "direction": d.get("direction"),
                "side": d.get("side"),
                "entry_range": d.get("entry_range"),
                "sl": d.get("sl"),
                "tp1": d.get("tp1"),
                "tp2": d.get("tp2"),
            }
        )
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "ETH": {
                        "asset": "ETH",
                        "flow_derivatives_context": {
                            "bias": "bullish",
                            "confidence": 0.82,
                            "crowding_state": "neutral",
                            "exchange_pressure": "low",
                            "stablecoin_support": "high",
                            "unlock_pressure": "low",
                            "drivers": ["stablecoin support is healthy"],
                            "summary": "Supportive long flow.",
                        },
                    }
                }
            )
        )

        get_signal_json.apply_signal_asset_flow_overlay(d, path)

        self.assertEqual(d.get("confidence"), "High")
        self.assertEqual(d.get("direction"), "long")
        self.assertEqual(d.get("side"), "long")
        self.assertEqual(d.get("asset_flow_summary", {}).get("flow_support_for_direction"), "supportive")
        self.assertEqual(d.get("asset_flow_summary", {}).get("execution_caution"), "low")
        self.assertEqual(
            {
                "direction": d.get("direction"),
                "side": d.get("side"),
                "entry_range": d.get("entry_range"),
                "sl": d.get("sl"),
                "tp1": d.get("tp1"),
                "tp2": d.get("tp2"),
            },
            before,
        )

    def test_long_bearish_or_long_crowded_flow_reduces_confidence_and_forces_wait_confirm(self) -> None:
        d = self._base_signal(side="long", confidence="High")
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "ETH": {
                        "asset": "ETH",
                        "flow_derivatives_context": {
                            "bias": "bearish",
                            "confidence": 0.79,
                            "crowding_state": "long_crowded",
                            "exchange_pressure": "high",
                            "stablecoin_support": "low",
                            "unlock_pressure": "high",
                            "drivers": ["selling pressure is building"],
                            "summary": "Adverse long flow.",
                        },
                    }
                }
            )
        )

        get_signal_json.apply_signal_asset_flow_overlay(d, path)

        self.assertEqual(d.get("confidence"), "Medium")
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertIn("flow_opposes_direction", d.get("warnings") or [])
        self.assertIn("dump_risk_long_crowded", d.get("warnings") or [])
        self.assertFalse(bool(d.get("no_trade")))

    def test_short_supportive_bearish_flow_can_improve_confidence(self) -> None:
        d = self._base_signal(symbol="SOL/USDT", side="short", confidence="Medium")
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "SOL": {
                        "asset": "SOL",
                        "flow_derivatives_context": {
                            "bias": "bearish",
                            "confidence": 0.76,
                            "crowding_state": "neutral",
                            "exchange_pressure": "high",
                            "stablecoin_support": "low",
                            "unlock_pressure": "medium",
                            "drivers": ["exchange inflows remain heavy"],
                            "summary": "Supportive short flow.",
                        },
                    }
                }
            )
        )

        get_signal_json.apply_signal_asset_flow_overlay(d, path)

        self.assertEqual(d.get("confidence"), "High")
        self.assertEqual(d.get("direction"), "short")
        self.assertEqual(d.get("asset_flow_summary", {}).get("flow_support_for_direction"), "supportive")

    def test_short_short_crowded_flow_adds_squeeze_warning_and_keeps_trade_allowed(self) -> None:
        d = self._base_signal(symbol="SOL/USDT", side="short", confidence="High")
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "SOL": {
                        "asset": "SOL",
                        "flow_derivatives_context": {
                            "bias": "neutral",
                            "confidence": 0.63,
                            "crowding_state": "short_crowded",
                            "exchange_pressure": "medium",
                            "stablecoin_support": "high",
                            "unlock_pressure": "low",
                            "drivers": ["short positioning is crowded"],
                            "summary": "Squeeze-prone short setup.",
                        },
                    }
                }
            )
        )

        get_signal_json.apply_signal_asset_flow_overlay(d, path)

        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertIn("squeeze_risk_short_crowded", d.get("warnings") or [])
        self.assertFalse(bool(d.get("no_trade")))

    def test_mixed_or_neutral_flow_is_caution_only(self) -> None:
        d = self._base_signal(side="long", confidence="High")
        d["price_vs_ema20_h1"] = "unknown"
        d["ema_fan_h1_state"] = "mixed"
        d["ema_fan_m15_state"] = "mixed"
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "ETH": {
                        "asset": "ETH",
                        "flow_derivatives_context": {
                            "bias": "neutral",
                            "confidence": 0.58,
                            "crowding_state": "mixed",
                            "exchange_pressure": "medium",
                            "stablecoin_support": "medium",
                            "unlock_pressure": "medium",
                            "drivers": ["two-sided regime persists"],
                            "summary": "Neutral, unstable follow-through.",
                        },
                    }
                }
            )
        )

        get_signal_json.apply_signal_asset_flow_overlay(d, path)

        self.assertEqual(d.get("confidence"), "High")
        self.assertEqual(d.get("entry_mode"), "limit")
        self.assertIn("mixed_positioning", d.get("warnings") or [])
        self.assertEqual(d.get("asset_flow_summary", {}).get("flow_support_for_direction"), "mixed")
        self.assertFalse(bool(d.get("no_trade")))

    def test_missing_asset_flow_preserves_existing_behavior(self) -> None:
        d = self._base_signal(symbol="DOGE/USDT", side="long", confidence="Medium")
        before = copy.deepcopy(d)
        path = self._write_snapshot(
            self._v2_snapshot(
                asset_contexts={
                    "ETH": {
                        "asset": "ETH",
                        "flow_derivatives_context": {
                            "bias": "bullish",
                            "confidence": 0.8,
                            "crowding_state": "neutral",
                            "exchange_pressure": "low",
                            "stablecoin_support": "high",
                            "unlock_pressure": "low",
                            "drivers": [],
                            "summary": "irrelevant",
                        },
                    }
                }
            )
        )

        get_signal_json.apply_signal_asset_flow_overlay(d, path)

        self.assertEqual(d, before)


if __name__ == "__main__":
    unittest.main()
