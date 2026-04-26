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
            with tempfile.TemporaryDirectory() as td:
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

                # Even with provenance overwrite muted, postprocess_full_last still recomputes EMA blocks
                # from fresh OHLCV via apply_ema_blocks_and_derivatives(). The test must therefore validate
                # consistency against final EMA truth, not against the seeded stale snapshot values.
                with patch.object(postprocess_full_last, "overwrite_ema20_from_provenance", side_effect=fake_overwrite):
                    postprocess_full_last.main()

                out = json.loads((base / "logs" / "last.json").read_text(encoding="utf-8"))
                price = float(out["price"])
                ema20_m15 = float(out["ema20_m15"])
                ema20_h1 = float(out["ema20_h1"])
                exp_m15 = "above" if price > ema20_m15 else ("below" if price < ema20_m15 else "equal")
                exp_h1 = "above" if price > ema20_h1 else ("below" if price < ema20_h1 else "equal")
                self.assertEqual(out.get("price_vs_ema20_m15"), exp_m15)
                self.assertEqual(out.get("price_vs_ema20_h1"), exp_h1)
                if price > ema20_m15 and price > ema20_h1:
                    exp_state = "above_both"
                elif price < ema20_m15 and price < ema20_h1:
                    exp_state = "below_both"
                else:
                    exp_state = "between"
                self.assertEqual(out.get("ema_guard_state"), exp_state)

                mtf = out.get("multi_tf_view") or {}
                if isinstance(mtf, dict):
                    m15_text = str(mtf.get("m15", ""))
                    h1_text = str(mtf.get("h1", ""))
                else:
                    self.assertIsInstance(mtf, str)
                    m15_text = str(mtf)
                    h1_text = str(mtf)
                if exp_m15 == "above":
                    self.assertNotRegex(m15_text, re.compile(r"(?i)(ниже|под)\\s+ema20"))
                elif exp_m15 == "below":
                    self.assertNotRegex(m15_text, re.compile(r"(?i)(выше|над)\\s+ema20"))
                else:
                    self.assertNotRegex(m15_text, re.compile(r"(?i)(выше|над|ниже|под)\\s+ema20"))
                if exp_h1 == "above":
                    self.assertNotRegex(h1_text, re.compile(r"(?i)(ниже|под)\\s+ema20"))
                elif exp_h1 == "below":
                    self.assertNotRegex(h1_text, re.compile(r"(?i)(выше|над)\\s+ema20"))
                else:
                    self.assertNotRegex(h1_text, re.compile(r"(?i)(выше|над|ниже|под)\\s+ema20"))

                # why_asset must remain consistent with final EMA guard state.
                why = str(out.get("why_asset") or "")
                if exp_state == "above_both":
                    self.assertNotRegex(why, re.compile(r"(?i)(ниже|под)\\s+ema20"))
                elif exp_state == "below_both":
                    self.assertNotRegex(why, re.compile(r"(?i)(выше|над)\\s+ema20"))
                else:
                    self.assertNotRegex(why, re.compile(r"(?i)(выше|над|ниже|под)\\s+ema20"))
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
            patch("sys.argv", ["render_strict.py"]),
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
            patch("sys.argv", ["render_strict.py"]),
            patch("render_strict.pathlib.Path.write_text", return_value=None),
            contextlib.redirect_stdout(buf_out),
        ):
            render_strict.main()
        rendered = buf_out.getvalue()

        self.assertNotRegex(rendered, re.compile(r"(?i)15m\\s*:\\s*.*выше\\s+ema20"))
        self.assertIn("EMA статус: M15: НИЖЕ EMA20", rendered)

    def test_string_multi_tf_view_is_replaced_with_ema_consistent_summary(self) -> None:
        d = {
            "price": 600.0,
            "ema20_m15": 605.0,
            "ema20_h1": 590.0,
            "multi_tf_view": "M15 и H1: цена выше EMA20/60, структура восходящая",
        }

        get_signal_json.enforce_ema_narrative_consistency(d)

        self.assertEqual(
            d.get("multi_tf_view"),
            "M15: цена ниже EMA20; H1: цена выше EMA20; по EMA20 структура смешанная, единого подтверждения нет.",
        )

    def test_why_asset_comparison_does_not_treat_h1_above_ema60_as_negative(self) -> None:
        d = {
            "why_asset": "BTC предпочтительнее, чем хай-бета/активы с H1 выше EMA60: структура чище.",
        }

        get_signal_json.enforce_ema_narrative_consistency(d)

        why = str(d.get("why_asset") or "")
        self.assertNotIn("активы с H1 выше EMA60", why)
        self.assertIn("смешанной H1-структурой или ниже EMA60", why)

    def test_render_guard_fixes_raw_string_multi_tf_view_without_postprocess(self) -> None:
        d = {
            "time_msk": "01.01.2025, 00:00",
            "symbol": "BNB/USDT",
            "price": 600.0,
            "ema20_m15": 605.0,
            "ema20_h1": 590.0,
            "mode": "neutral",
            "why_asset": "test",
            "multi_tf_view": "M15 и H1: цена выше EMA20/60, структура восходящая",
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 599.0,
            "sl_by_mode": {"neutral": 590.0},
            "tp_by_mode": {"neutral": {"tvh1": 610.0, "tvh2": 620.0}},
            "rr_by_mode": {"neutral": 1.2},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        buf_out = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(d))),
            patch("sys.argv", ["render_strict.py"]),
            patch("render_strict.pathlib.Path.write_text", return_value=None),
            contextlib.redirect_stdout(buf_out),
        ):
            render_strict.main()
        rendered = buf_out.getvalue()

        self.assertIn("EMA статус: M15: НИЖЕ EMA20 | H1: ВЫШЕ EMA20", rendered)
        self.assertIn(
            "Таймфреймы: M15: цена ниже EMA20; H1: цена выше EMA20; по EMA20 структура смешанная, единого подтверждения нет.",
            rendered,
        )
        self.assertNotIn("цена выше EMA20/60, структура восходящая", rendered)


if __name__ == "__main__":
    unittest.main()
