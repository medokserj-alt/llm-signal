import contextlib
import io
import json
import unittest
from unittest.mock import patch

import render_strict


def _render_text(data: dict) -> str:
    buf_out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data, ensure_ascii=False))),
        patch("sys.argv", ["render_strict.py"]),
        patch("render_strict.pathlib.Path.write_text", return_value=None),
        contextlib.redirect_stdout(buf_out),
    ):
        render_strict.main()
    return buf_out.getvalue()


class TestRenderEventRisk(unittest.TestCase):
    def test_render_mentions_event_name_time_and_caution(self) -> None:
        d = {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "aggressive",
            "side": "long",
            "entry_mode": "wait_confirm",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"aggressive": {"enabled": True}},
            "entry_price_aggressive": 99.5,
            "sl_by_mode": {"aggressive": 98.0},
            "tp_by_mode": {"aggressive": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"aggressive": 1.8},
            "exit_plan_by_mode": {"aggressive": "plan"},
            "event_risk": {
                "display_lines": [
                    "⚠️ Event risk: Trump press conference (04.04.2026, 16:00 МСК) — активное окно high-impact события; aggressive переведён в wait_confirm."
                ]
            },
        }

        out = _render_text(d)
        self.assertIn("Trump press conference", out)
        self.assertIn("16:00 МСК", out)
        self.assertIn("wait_confirm", out)

    def test_render_accepts_string_multi_tf_view_without_traceback(self) -> None:
        d = {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": "Trend mixed; 15m reclaim attempt, 1h range.",
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 99.5,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"neutral": 1.8},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = _render_text(d)
        self.assertIn("3️⃣ Таймфреймы", out)
        self.assertIn("Таймфреймы: Trend mixed; 15m reclaim attempt, 1h range.", out)

    def test_render_news_context_dicts_and_partial_items_without_list_repr(self) -> None:
        d = {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "impulse", "m15": "trend", "h1": "support", "h4": "range", "d1": "bull"},
            "news_context": [
                {
                    "title": "US jobs data beat expectations",
                    "impact": "+",
                    "time_msk": "04.04.2026 15:30",
                    "url": "https://example.com/jobs",
                    "summary": "Dollar bid strengthened after the release.",
                },
                {"title": "partial payload"},
            ],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 99.5,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"neutral": 1.8},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = _render_text(d)
        self.assertIn(
            "[04.04.2026 15:30] [impact:+] US jobs data beat expectations — https://example.com/jobs — Dollar bid strengthened after the release.",
            out,
        )
        self.assertIn("{'title': 'partial payload'}", out)
        self.assertNotIn("[\n", out)

    def test_render_empty_news_context_as_dash(self) -> None:
        d = {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "impulse", "m15": "trend", "h1": "support", "h4": "range", "d1": "bull"},
            "news_context": None,
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 99.5,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"neutral": 1.8},
            "exit_plan_by_mode": {"neutral": "plan"},
        }

        out = _render_text(d)
        self.assertIn("Новостной фон: —", out)

    def test_render_mtf_all_dashes_uses_directional_fallback(self) -> None:
        d = {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "mode": "neutral",
            "side": "long",
            "why_asset": "test",
            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
            "news_context": [],
            "entries": {"neutral": {"enabled": True}},
            "entry_price_neutral": 99.5,
            "sl_by_mode": {"neutral": 98.0},
            "tp_by_mode": {"neutral": {"tvh1": 102.0, "tvh2": 104.0}},
            "rr_by_mode": {"neutral": 1.8},
            "exit_plan_by_mode": {"neutral": "plan"},
            "price_vs_ema20_m15": "above",
        }

        out = _render_text(d)
        self.assertIn(
            "Таймфреймы: 5m–1h: структура соответствует направлению сделки, откаты к EMA используются как точки входа.",
            out,
        )
        self.assertNotIn("5m: —; 15m: —; 1h: —; 4h: —; 1D: —", out)


if __name__ == "__main__":
    unittest.main()
