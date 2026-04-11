import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import get_signal_json
import postprocess_full_last


class TestMacroEventFieldsPreserved(unittest.TestCase):
    def _base_signal(self) -> dict:
        return {
            "time_msk": "01.01.2025, 12:00",
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
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
        }

    def test_finalize_signal_preserves_macro_event_fields(self) -> None:
        d = self._base_signal()
        d["upcoming_events"] = [
            {
                "time_msk": "05.04.2026, 15:30",
                "date_msk": "05.04.2026",
                "event": "US CPI",
                "category": "macro",
                "impact": "high",
                "window_before_min": 30,
                "window_after_min": 60,
                "expected_regime_effect": "volatility",
                "note": "High vol expected",
            },
            {
                "event": "Powell remarks",
                "impact": "medium",
                "note": "Keep size smaller",
            },
        ]
        d["macro_risk_summary"] = "Clustered macro risk around the US session open."

        out = get_signal_json.finalize_signal(d, hints={}, fetch_price=False)

        self.assertEqual(
            out.get("macro_risk_summary"),
            "Clustered macro risk around the US session open.",
        )
        self.assertEqual(len(out.get("upcoming_events") or []), 2)
        self.assertEqual(out["upcoming_events"][0]["event"], "US CPI")
        self.assertEqual(out["upcoming_events"][0]["window_before_min"], 30)
        self.assertEqual(out["upcoming_events"][1]["event"], "Powell remarks")

    def test_finalize_signal_defaults_absent_macro_event_fields(self) -> None:
        out = get_signal_json.finalize_signal(self._base_signal(), hints={}, fetch_price=False)
        self.assertEqual(out.get("upcoming_events"), [])
        self.assertEqual(out.get("macro_risk_summary"), "")

    def test_postprocess_full_last_preserves_macro_event_fields(self) -> None:
        in_data = self._base_signal()
        in_data["upcoming_events"] = [
            {
                "date_msk": "01.01.2025",
                "time_msk": "TBD",
                "event": "Fed speaker",
                "category": "fed",
                "impact": "medium",
                "note": "Headline risk",
            }
        ]
        in_data["macro_risk_summary"] = "Fed headlines can interrupt continuation."

        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            base = Path(td)
            (base / "logs").mkdir(parents=True, exist_ok=True)
            (base / "logs" / "last.json").write_text(
                json.dumps(in_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            old_base = postprocess_full_last.BASE
            try:
                postprocess_full_last.BASE = base
                with patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=lambda _d: None):
                    postprocess_full_last.main()
            finally:
                postprocess_full_last.BASE = old_base

            out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(out.get("macro_risk_summary"), "Fed headlines can interrupt continuation.")
            self.assertEqual(len(out.get("upcoming_events") or []), 1)
            self.assertEqual(out["upcoming_events"][0]["event"], "Fed speaker")

    def test_postprocess_full_last_defaults_absent_macro_event_fields(self) -> None:
        in_data = self._base_signal()

        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            base = Path(td)
            (base / "logs").mkdir(parents=True, exist_ok=True)
            (base / "logs" / "last.json").write_text(
                json.dumps(in_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            old_base = postprocess_full_last.BASE
            try:
                postprocess_full_last.BASE = base
                with patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=lambda _d: None):
                    postprocess_full_last.main()
            finally:
                postprocess_full_last.BASE = old_base

            out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(out.get("upcoming_events"), [])
            self.assertEqual(out.get("macro_risk_summary"), "")

    def test_postprocess_full_last_reads_day_report_macro_context_into_signal(self) -> None:
        in_data = self._base_signal()
        in_data["upcoming_events"] = []
        in_data["macro_risk_summary"] = ""

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
                        "time_msk": "04.04.2026, 14:00",
                        "upcoming_events": [
                            {
                                "date_msk": "04.04.2026",
                                "time_msk": "16:00",
                                "event": "Trump press conference",
                                "impact": "high",
                            }
                        ],
                        "macro_risk_summary": "Trump headlines may spike volatility into the close.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (base / "reports" / "mid" / "20260404_120000" / "last.json").write_text(
                json.dumps({"time_msk": "04.04.2026, 10:00"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            old_base = postprocess_full_last.BASE
            old_gsj_base = get_signal_json.BASE
            try:
                postprocess_full_last.BASE = base
                get_signal_json.BASE = base
                with patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=lambda _d: None):
                    postprocess_full_last.main()
            finally:
                postprocess_full_last.BASE = old_base
                get_signal_json.BASE = old_gsj_base

            out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(out.get("macro_risk_summary"), "Trump headlines may spike volatility into the close.")
            self.assertEqual(len(out.get("upcoming_events") or []), 1)
            self.assertEqual(out["upcoming_events"][0]["event"], "Trump press conference")
            ctx = out.get("day_mid_context") or {}
            self.assertEqual(len(ctx.get("upcoming_events") or []), 1)
            self.assertEqual(ctx["upcoming_events"][0]["event"], "Trump press conference")


if __name__ == "__main__":
    unittest.main()
