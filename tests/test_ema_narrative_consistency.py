import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import get_signal_json
import postprocess_full_last
import render_strict


class TestEMANarrativeConsistency(unittest.TestCase):
    def test_postprocess_adds_flags_and_fixes_m15_claim(self) -> None:
        old_base = postprocess_full_last.BASE

        def fake_overwrite(_d: dict) -> None:
            return

        try:
            with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
                base = Path(td)
                postprocess_full_last.BASE = base
                (base / "logs").mkdir(parents=True, exist_ok=True)

                in_data = {
                    "symbol": "ADA/USDT",
                    "price": 0.3692,
                    "ema20_m15": 0.372222,  # price below
                    "ema20_h1": 0.3680,  # price above -> between
                    "why_asset": "В целом цена выше EMA20, структура неплохая.",  # intentionally wrong
                    "multi_tf_view": {
                        "m5": "—",
                        "m15": "15m: цена выше EMA20, локальный импульс.",  # intentionally wrong
                        "h1": "1h: цена выше EMA20, контекст норм.",
                        "h4": "—",
                        "d1": "—",
                    },
                    "entries": {"neutral": {"enabled": True, "range": {"min": 1, "max": 2}}},
                    "no_trade": False,
                    "no_trade_reasons": [],
                    "no_trade_hint": "",
                    "mode": "neutral",
                    # minimal by-mode fields for postprocess_full_last
                    "sl_by_mode": {"neutral": 0.36},
                    "tp_by_mode": {"neutral": {"tvh1": 0.38, "tvh2": 0.39}},
                    "rr_by_mode": {"neutral": 1.2},
                    "exit_plan_by_mode": {"neutral": "plan"},
                }
                (base / "logs" / "last.json").write_text(
                    json.dumps(in_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                # For this test we want to validate narrative fixes using the explicitly provided EMA values
                # without overriding them from provenance.
                with patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=fake_overwrite):
                    postprocess_full_last.main()

                out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
                self.assertEqual(out.get("price_vs_ema20_m15"), "below")
                price = float(out["price"])
                ema20_h1 = float(out["ema20_h1"])
                exp_h1 = "above" if price > ema20_h1 else ("below" if price < ema20_h1 else "equal")
                self.assertEqual(out.get("price_vs_ema20_h1"), exp_h1)
                ema20_m15 = float(out["ema20_m15"])
                if price > ema20_m15 and price > ema20_h1:
                    exp_state = "above_both"
                elif price < ema20_m15 and price < ema20_h1:
                    exp_state = "below_both"
                else:
                    exp_state = "between"
                self.assertEqual(out.get("ema_guard_state"), exp_state)

                mtf = out.get("multi_tf_view") or {}
                self.assertIsInstance(mtf, dict)
                self.assertNotRegex(str(mtf.get("m15", "")), re.compile(r"(?i)выше\\s+ema20"))

                # between -> generic "выше/ниже EMA20" in why_asset must be neutralized
                why = str(out.get("why_asset") or "")
                self.assertNotRegex(why, re.compile(r"(?i)выше\\s+ema20"))
        finally:
            postprocess_full_last.BASE = old_base

    def test_render_includes_compact_ema_status_line(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "ADA/USDT",
            "price": 0.3692,
            "mode": "neutral",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 0.37,
            "sl_by_mode": {"neutral": 0.36},
            "tp_by_mode": {"neutral": {"tvh1": 0.38, "tvh2": 0.39}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
            "price_vs_ema20_m15": "below",
            "price_vs_ema20_h1": "above",
        }

        buf_out = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(d))),
            patch("render_strict.pathlib.Path.write_text", return_value=None),
            contextlib.redirect_stdout(buf_out),
        ):
            render_strict.main()
        out = buf_out.getvalue()
        self.assertIn("EMA статус: M15: НИЖЕ EMA20 | H1: ВЫШЕ EMA20", out)

    def test_rendered_output_does_not_claim_m15_above_when_below(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "ADA/USDT",
            "price": 0.3692,
            "ema20_m15": 0.372222,  # price below
            "ema20_h1": 0.3680,  # price above
            "mode": "neutral",
            "why_asset": "test",
            # intentionally wrong: must be corrected before rendering
            "multi_tf_view": {
                "m5": "—",
                "m15": "цена выше EMA20",
                "h1": "—",
                "h4": "—",
                "d1": "—",
            },
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 0.37,
            "sl_by_mode": {"neutral": 0.36},
            "tp_by_mode": {"neutral": {"tvh1": 0.38, "tvh2": 0.39}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        get_signal_json.apply_ema_relation_flags(d)
        get_signal_json.enforce_ema_narrative_consistency(d)

        buf_out = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(d))),
            patch("render_strict.pathlib.Path.write_text", return_value=None),
            contextlib.redirect_stdout(buf_out),
        ):
            render_strict.main()
        rendered = buf_out.getvalue()

        self.assertNotRegex(rendered, re.compile(r"(?i)15m\\s*:\\s*.*выше\\s+ema20"))
        self.assertIn("EMA статус: M15: НИЖЕ EMA20", rendered)


if __name__ == "__main__":
    unittest.main()
