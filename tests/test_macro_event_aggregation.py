import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import get_signal_json
import postprocess_full_last


class TestMacroEventAggregation(unittest.TestCase):
    def _base_signal(self) -> dict:
        return {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "direction": "long",
            "side": "long",
            "mode": "neutral",
            "entry_mode": "limit",
            "entry_range": {"min": 99.0, "max": 100.0},
            "sl_by_mode": {"neutral": 95.0},
            "tp_by_mode": {"neutral": {"tvh1": 103.0, "tvh2": 106.0}},
            "rr_by_mode": {"neutral": 1.5},
            "exit_plan_by_mode": {"neutral": "plan"},
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "why_asset": "test",
            "news_context": [],
            "market_context": "test",
            "validity_minutes": 90,
            "cancel_condition": "test",
            "technical_rationale": "test",
            "confidence": "High",
            "warnings": [],
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "upcoming_events": [],
            "macro_risk_summary": "",
            "day_mid_context": {
                "day_bias": "long",
                "mid_bias": "long",
                "notes": "Existing context note",
                "upcoming_events": [],
                "macro_risk_summary": "",
            },
        }

    def test_merge_day_mid_report_context_deduplicates_and_keeps_day_then_mid_order(self) -> None:
        d = self._base_signal()
        d["upcoming_events"] = [
            {
                "event": "U.S. CPI",
                "category": "macro",
                "impact": "high",
                "date_msk": "05.04.2026",
                "time_msk": "15:30",
            }
        ]
        d["macro_risk_summary"] = "US CPI can spike volatility."
        d["day_mid_context"]["upcoming_events"] = [
            {
                "event": "US CPI",
                "category": "macro",
                "impact": "high",
                "date_msk": "05.04.2026",
                "time_msk": "15:30",
                "note": "Size down into the print",
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = (
            "US CPI can spike volatility. Powell remarks can extend headline risk."
        )

        day_report = {
            "time_msk": "04.04.2026, 09:00",
            "upcoming_events": [
                {
                    "event": "US CPI",
                    "category": "macro",
                    "impact": "high",
                    "date_msk": "05.04.2026",
                    "time_msk": "15:30",
                }
            ],
            "macro_risk_summary": "US CPI can spike volatility.",
            "day_mid_context": {"notes": "DAY note"},
        }
        mid_report = {
            "time_msk": "04.04.2026, 12:00",
            "upcoming_events": [
                {
                    "event": "Powell remarks",
                    "category": "fed",
                    "impact": "medium",
                    "date_msk": "05.04.2026",
                    "time_msk": "18:00",
                    "note": "Keep size smaller",
                },
                {
                    "event": "  U.S. CPI ",
                    "category": "macro",
                    "impact": "high",
                    "date_msk": "05.04.2026",
                    "time_msk": "15:30",
                },
            ],
            "macro_risk_summary": "Powell remarks can extend headline risk.",
            "day_mid_context": {"notes": "MID note"},
        }

        get_signal_json.merge_day_mid_report_context(
            d,
            day_report=day_report,
            mid_report=mid_report,
        )

        events = d.get("upcoming_events") or []
        self.assertIsInstance(events, list)
        self.assertEqual([item["event"] for item in events], ["US CPI", "Powell remarks"])
        self.assertEqual(events[0]["note"], "Size down into the print")
        self.assertEqual(
            d.get("macro_risk_summary"),
            "US CPI can spike volatility. Powell remarks can extend headline risk.",
        )
        self.assertEqual(
            [item["event"] for item in (d.get("day_mid_context") or {}).get("upcoming_events") or []],
            ["US CPI", "Powell remarks"],
        )
        self.assertEqual(
            (d.get("day_mid_context") or {}).get("macro_risk_summary"),
            "US CPI can spike volatility. Powell remarks can extend headline risk.",
        )
        self.assertEqual((d.get("day_mid_context") or {}).get("notes"), "DAY note MID note Existing context note")
        for item in events:
            self.assertTrue(all(value is not None for value in item.values()))

    def test_postprocess_full_last_preserves_deduplicated_macro_risk_block(self) -> None:
        in_data = self._base_signal()
        in_data["upcoming_events"] = [
            {
                "event": "US CPI",
                "category": "macro",
                "impact": "high",
                "date_msk": "05.04.2026",
                "time_msk": "15:30",
                "note": "Size down into the print",
            },
            {
                "event": "Powell remarks",
                "category": "fed",
                "impact": "medium",
                "date_msk": "05.04.2026",
                "time_msk": "18:00",
                "note": "Keep size smaller",
            },
        ]
        in_data["macro_risk_summary"] = (
            "US CPI can spike volatility. Powell remarks can extend headline risk."
        )
        in_data["day_mid_context"]["upcoming_events"] = list(in_data["upcoming_events"])
        in_data["day_mid_context"]["macro_risk_summary"] = in_data["macro_risk_summary"]

        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            base = Path(td)
            (base / "logs").mkdir(parents=True, exist_ok=True)
            (base / "reports" / "day" / "20260404_120000").mkdir(parents=True, exist_ok=True)
            (base / "reports" / "mid" / "20260404_120000").mkdir(parents=True, exist_ok=True)

            (base / "logs" / "last.json").write_text(
                json.dumps(in_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (base / "reports" / "day" / "20260404_120000" / "last.json").write_text(
                json.dumps(
                    {
                        "time_msk": "04.04.2026, 09:00",
                        "upcoming_events": [
                            {
                                "event": "US CPI",
                                "category": "macro",
                                "impact": "high",
                                "date_msk": "05.04.2026",
                                "time_msk": "15:30",
                            }
                        ],
                        "macro_risk_summary": "US CPI can spike volatility.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (base / "reports" / "mid" / "20260404_120000" / "last.json").write_text(
                json.dumps(
                    {
                        "time_msk": "04.04.2026, 12:00",
                        "upcoming_events": [
                            {
                                "event": "  U.S. CPI ",
                                "category": "macro",
                                "impact": "high",
                                "date_msk": "05.04.2026",
                                "time_msk": "15:30",
                            },
                            {
                                "event": "Powell remarks",
                                "category": "fed",
                                "impact": "medium",
                                "date_msk": "05.04.2026",
                                "time_msk": "18:00",
                                "note": "Keep size smaller",
                            },
                        ],
                        "macro_risk_summary": "Powell remarks can extend headline risk.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            old_postprocess_base = postprocess_full_last.BASE
            old_signal_base = get_signal_json.BASE
            try:
                postprocess_full_last.BASE = base
                get_signal_json.BASE = base
                with patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=lambda _d: None):
                    postprocess_full_last.main()
            finally:
                postprocess_full_last.BASE = old_postprocess_base
                get_signal_json.BASE = old_signal_base

            out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(
                out.get("macro_risk_summary"),
                "US CPI can spike volatility. Powell remarks can extend headline risk.",
            )
            self.assertEqual(
                [item["event"] for item in out.get("upcoming_events") or []],
                ["US CPI", "Powell remarks"],
            )
            self.assertEqual(len(out.get("upcoming_events") or []), 2)
            ctx = out.get("day_mid_context") or {}
            self.assertEqual(
                [item["event"] for item in ctx.get("upcoming_events") or []],
                ["US CPI", "Powell remarks"],
            )
            self.assertEqual(
                ctx.get("macro_risk_summary"),
                "US CPI can spike volatility. Powell remarks can extend headline risk.",
            )

    def test_event_note_collapse_keeps_unique_semantic_fragments_only(self) -> None:
        d = self._base_signal()
        d["upcoming_events"] = [
            {
                "event": "Thin liquidity in Asia and Europe",
                "category": "market_structure",
                "impact": "medium",
                "date_msk": "05.04.2026",
                "time_msk": "TBD",
                "note": (
                    "Thin liquidity can trigger false breakouts. "
                    "Thin liquidity can trigger false breakouts near the open."
                ),
            }
        ]
        d["day_mid_context"]["upcoming_events"] = [
            {
                "event": "Thin liquidity in Asia and Europe",
                "category": "market_structure",
                "impact": "medium",
                "date_msk": "05.04.2026",
                "time_msk": "05.04.2026, TBD",
                "note": (
                    "Thin liquidity can trigger false breakouts near the open. "
                    "Wait for reclaim after the spike."
                ),
            }
        ]

        get_signal_json.merge_day_mid_report_context(d)

        events = d.get("upcoming_events") or []
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].get("note"),
            "Thin liquidity can trigger false breakouts near the open. Wait for reclaim after the spike.",
        )

    def test_macro_risk_summary_collapse_removes_repeated_meaning(self) -> None:
        d = self._base_signal()
        d["macro_risk_summary"] = (
            "US CPI can spike volatility. "
            "US CPI can spike volatility into the US open. "
            "Powell remarks can extend headline risk. "
            "Powell remarks can extend headline risk into the close."
        )

        get_signal_json.ensure_macro_event_fields(d)

        self.assertEqual(
            d.get("macro_risk_summary"),
            "US CPI can spike volatility into the US open. Powell remarks can extend headline risk into the close.",
        )

    def test_tbd_and_dated_tbd_events_collapse_to_single_more_informative_entry(self) -> None:
        d = self._base_signal()
        d["upcoming_events"] = []
        d["day_mid_context"]["upcoming_events"] = []

        day_report = {
            "time_msk": "04.04.2026, 09:00",
            "upcoming_events": [
                {
                    "event": "Thin liquidity in Asia and Europe",
                    "category": "market_structure",
                    "impact": "medium",
                    "date_msk": "05.04.2026",
                    "time_msk": "TBD",
                    "note": "Thin liquidity can trigger false breakouts.",
                }
            ],
        }
        mid_report = {
            "time_msk": "04.04.2026, 12:00",
            "upcoming_events": [
                {
                    "event": "Thin liquidity in Asia and Europe",
                    "category": "market_structure",
                    "impact": "medium",
                    "date_msk": "05.04.2026",
                    "time_msk": "05.04.2026, TBD",
                    "note": "Prefer confirmation before entry.",
                }
            ],
        }

        get_signal_json.merge_day_mid_report_context(
            d,
            day_report=day_report,
            mid_report=mid_report,
        )

        events = d.get("upcoming_events") or []
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("time_msk"), "05.04.2026, TBD")
        self.assertEqual(
            events[0].get("note"),
            "Thin liquidity can trigger false breakouts. Prefer confirmation before entry.",
        )

    def test_semantic_duplicate_politics_and_market_structure_events_merge_into_single_richer_records(self) -> None:
        d = self._base_signal()
        d["upcoming_events"] = [
            {
                "event": "Iran headline risk",
                "category": "politics",
                "impact": "high",
                "note": "Middle East headline risk.",
            },
            {
                "event": "BTC liquidation cluster above local highs",
                "category": "market_structure",
                "impact": "medium",
                "note": "Liquidation pocket can force a wick.",
            },
        ]
        d["day_mid_context"]["upcoming_events"] = [
            {
                "event": "Iran headline risk",
                "category": "politics",
                "impact": "high",
                "date_msk": "05.04.2026",
                "expected_regime_effect": "Risk-off impulse possible across majors.",
                "note": "Fade the first spike.",
            },
            {
                "event": "BTC liquidation cluster above local highs",
                "category": "market_structure",
                "impact": "medium",
                "date_msk": "05.04.2026",
                "time_msk": "05.04.2026, TBD",
                "window_before_min": 30,
                "window_after_min": 45,
                "expected_regime_effect": "Stop-driven sweep risk around local highs.",
                "note": "Wait for reclaim after the sweep.",
            },
        ]

        get_signal_json.merge_day_mid_report_context(d)

        events = d.get("upcoming_events") or []
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [item["event"] for item in events],
            ["Iran headline risk", "BTC liquidation cluster above local highs"],
        )

        iran_event = events[0]
        self.assertEqual(iran_event.get("category"), "politics")
        self.assertEqual(iran_event.get("date_msk"), "05.04.2026")
        self.assertEqual(
            iran_event.get("expected_regime_effect"),
            "Risk-off impulse possible across majors.",
        )
        self.assertEqual(
            iran_event.get("note"),
            "Fade the first spike. Middle East headline risk.",
        )

        btc_event = events[1]
        self.assertEqual(btc_event.get("category"), "market_structure")
        self.assertEqual(btc_event.get("date_msk"), "05.04.2026")
        self.assertEqual(btc_event.get("time_msk"), "05.04.2026, TBD")
        self.assertEqual(btc_event.get("window_before_min"), 30)
        self.assertEqual(btc_event.get("window_after_min"), 45)
        self.assertEqual(
            btc_event.get("expected_regime_effect"),
            "Stop-driven sweep risk around local highs.",
        )
        self.assertEqual(
            btc_event.get("note"),
            "Wait for reclaim after the sweep. Liquidation pocket can force a wick.",
        )


if __name__ == "__main__":
    unittest.main()
