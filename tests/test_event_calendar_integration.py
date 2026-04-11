import unittest

import get_signal_json


class TestEventCalendarIntegration(unittest.TestCase):
    def _calendar_context(self) -> dict:
        return {
            "generated_at_utc": "2026-04-04T08:00:00Z",
            "calendar_events": [
                {
                    "time_msk": "05.04.2026, 15:30",
                    "date_msk": "05.04.2026",
                    "event": "US CPI",
                    "category": "macro",
                    "impact": "high",
                    "window_before_min": 90,
                    "window_after_min": 120,
                    "source": "auto",
                    "note": "Macro release can reset volatility and risk appetite.",
                }
            ],
        }

    def test_day_mid_prompt_block_includes_calendar_input_contract(self) -> None:
        block = get_signal_json.build_event_calendar_prompt_block(
            "day",
            now_msk="04.04.2026, 14:30",
            calendar_context=self._calendar_context(),
        )

        self.assertIn("PRIMARY SCHEDULED TIMING SOURCE", block)
        self.assertIn("calendar_events", block)
        self.assertIn("US CPI", block)
        self.assertIn("must appear in upcoming_events", block)

    def test_calendar_backed_events_survive_into_upcoming_events(self) -> None:
        d = {
            "upcoming_events": [],
            "macro_risk_summary": "",
            "day_mid_context": {
                "day_bias": None,
                "mid_bias": None,
                "notes": None,
                "upcoming_events": [],
                "macro_risk_summary": "",
            },
        }

        get_signal_json.merge_event_calendar_context(
            d,
            profile="day",
            now_msk="04.04.2026, 14:30",
            calendar_context=self._calendar_context(),
        )

        self.assertEqual(len(d.get("upcoming_events") or []), 1)
        self.assertEqual(d["upcoming_events"][0]["event"], "US CPI")
        self.assertIn("US CPI", d.get("macro_risk_summary") or "")
        ctx = d.get("day_mid_context") or {}
        self.assertEqual(len(ctx.get("upcoming_events") or []), 1)
        self.assertEqual(ctx["upcoming_events"][0]["event"], "US CPI")

    def test_signal_event_risk_logic_works_with_calendar_backed_events(self) -> None:
        d = {
            "time_msk": "05.04.2026, 14:45",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "direction": "long",
            "side": "long",
            "mode": "aggressive",
            "entry_mode": "market",
            "confidence": "High",
            "warnings": [],
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "rr_by_mode": {"aggressive": 2.2},
            "upcoming_events": [],
            "macro_risk_summary": "",
            "day_mid_context": {
                "day_bias": "long",
                "mid_bias": "long",
                "notes": "DAY context",
                "upcoming_events": [],
                "macro_risk_summary": "",
            },
        }

        get_signal_json.merge_event_calendar_context(
            d,
            profile="signal",
            now_msk=d["time_msk"],
            calendar_context=self._calendar_context(),
        )
        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual(d.get("confidence"), "Medium")
        self.assertIn("upcoming_high_impact_event_wait_confirm", d.get("warnings") or [])
        self.assertIn("US CPI", " ".join((d.get("event_risk") or {}).get("display_lines") or []))

    def test_render_calendar_section_outputs_dedicated_block(self) -> None:
        rendered = get_signal_json.render_calendar_section(self._calendar_context()["calendar_events"])

        self.assertIn("🗓 Ключевые события периода", rendered)
        self.assertIn("05.04.2026, 15:30", rendered)
        self.assertIn("US CPI", rendered)
        self.assertIn("impact: high", rendered)


if __name__ == "__main__":
    unittest.main()
