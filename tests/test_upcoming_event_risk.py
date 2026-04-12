import unittest

import get_signal_json


class TestUpcomingEventRisk(unittest.TestCase):
    def _base_signal(self, mode: str) -> dict:
        return {
            "time_msk": "04.04.2026, 14:30",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "direction": "long",
            "side": "long",
            "mode": mode,
            "entry_mode": "limit",
            "confidence": "High",
            "warnings": [],
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "rr_by_mode": {mode: 2.2},
            "day_mid_context": {
                "day_bias": "long",
                "mid_bias": "long",
                "notes": "DAY context",
                "upcoming_events": [
                    {
                        "date_msk": "04.04.2026",
                        "time_msk": "16:00",
                        "event": "Trump press conference",
                        "impact": "high",
                        "category": "politics",
                    }
                ],
                "macro_risk_summary": "Trump headlines may spike volatility into the close.",
            },
        }

    def test_aggressive_high_impact_event_within_90_min_forces_wait_confirm(self) -> None:
        d = self._base_signal("aggressive")
        d["entry_mode"] = "market"

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual(d.get("confidence"), "Medium")
        self.assertIn("upcoming_high_impact_event_wait_confirm", d.get("warnings") or [])
        self.assertEqual(len(d.get("upcoming_events") or []), 1)
        event_risk = d.get("event_risk") or {}
        self.assertEqual(event_risk.get("mode_action"), "wait_confirm")
        self.assertIn("Trump press conference", " ".join(event_risk.get("display_lines") or []))

    def test_neutral_high_impact_event_becomes_more_cautious(self) -> None:
        d = self._base_signal("neutral")

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual(d.get("confidence"), "Medium")
        self.assertIn("upcoming_high_impact_event_neutral_caution", d.get("warnings") or [])
        self.assertEqual((d.get("event_risk") or {}).get("mode_action"), "wait_confirm")

    def test_conservative_high_impact_event_enforces_wait_confirm_without_no_trade(self) -> None:
        d = self._base_signal("conservative")

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual(d.get("confidence"), "Medium")
        self.assertIn("upcoming_high_impact_event_conservative_caution", d.get("warnings") or [])
        self.assertEqual((d.get("event_risk") or {}).get("mode_action"), "wait_confirm")
        self.assertIn("Trump press conference", " ".join((d.get("event_risk") or {}).get("display_lines") or []))

    def test_tbd_event_does_not_hard_block_but_lowers_confidence(self) -> None:
        d = self._base_signal("aggressive")
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "04.04.2026",
                "time_msk": "TBD",
                "event": "Trump headline risk",
                "impact": "high",
                "category": "politics",
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "limit")
        self.assertEqual(d.get("confidence"), "Medium")
        self.assertIn("upcoming_event_tbd_macro_caution", d.get("warnings") or [])
        self.assertIn("Trump headline risk", " ".join((d.get("event_risk") or {}).get("display_lines") or []))

    def test_past_event_outside_window_is_removed_before_risk_logic(self) -> None:
        d = self._base_signal("aggressive")
        d["time_msk"] = "11.04.2026, 12:00"
        d["entry_mode"] = "limit"
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "06.04.2026",
                "time_msk": "06.04.2026, 18:00",
                "event": "Iran 48h ultimatum",
                "impact": "high",
                "category": "politics",
                "window_after_min": 120,
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = "Iran 48h ultimatum may spike weekend volatility."

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(d.get("upcoming_events"), [])
        self.assertEqual((d.get("day_mid_context") or {}).get("upcoming_events"), [])
        self.assertEqual(d.get("macro_risk_summary"), "")
        self.assertEqual((d.get("day_mid_context") or {}).get("macro_risk_summary"), "")
        self.assertEqual(d.get("entry_mode"), "limit")
        self.assertEqual(d.get("confidence"), "High")
        self.assertNotIn("upcoming_event_tbd_macro_caution", d.get("warnings") or [])
        self.assertNotIn("Iran 48h ultimatum", " ".join((d.get("event_risk") or {}).get("display_lines") or []))

    def test_past_event_inside_window_after_is_kept(self) -> None:
        d = self._base_signal("aggressive")
        d["time_msk"] = "04.04.2026, 16:30"
        d["entry_mode"] = "market"
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "04.04.2026",
                "time_msk": "04.04.2026, 16:00",
                "event": "US CPI",
                "impact": "high",
                "category": "macro",
                "window_after_min": 45,
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = "US CPI can keep volatility elevated after the release."

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(len(d.get("upcoming_events") or []), 1)
        self.assertEqual(d["upcoming_events"][0]["event"], "US CPI")
        self.assertIn("US CPI", d.get("macro_risk_summary") or "")
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertIn("upcoming_high_impact_event_wait_confirm", d.get("warnings") or [])

    def test_future_event_is_kept(self) -> None:
        d = self._base_signal("aggressive")
        d["time_msk"] = "04.04.2026, 12:00"
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "04.04.2026",
                "time_msk": "04.04.2026, 18:00",
                "event": "FOMC Minutes",
                "impact": "high",
                "category": "macro",
                "window_before_min": 30,
                "window_after_min": 60,
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = "FOMC Minutes are the main evening macro risk."

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(len(d.get("upcoming_events") or []), 1)
        self.assertEqual(d["upcoming_events"][0]["event"], "FOMC Minutes")
        self.assertEqual(d.get("macro_risk_summary"), "FOMC Minutes are the main evening macro risk.")
        self.assertEqual(d.get("entry_mode"), "limit")
        self.assertEqual(d.get("confidence"), "Medium")

    def test_old_tbd_event_is_removed(self) -> None:
        d = self._base_signal("aggressive")
        d["time_msk"] = "11.04.2026, 12:00"
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "06.04.2026",
                "time_msk": "TBD",
                "event": "Iran response deadline",
                "impact": "high",
                "category": "politics",
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = "Iran response deadline keeps headline risk elevated."

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(d.get("upcoming_events"), [])
        self.assertEqual(d.get("macro_risk_summary"), "")
        self.assertEqual(d.get("confidence"), "High")
        self.assertNotIn("Iran response deadline", " ".join((d.get("event_risk") or {}).get("display_lines") or []))


if __name__ == "__main__":
    unittest.main()
