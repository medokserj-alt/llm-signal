import copy
import unittest

import get_signal_json
from event_risk_context import build_event_risk_context


class TestSignalEventRiskIntegration(unittest.TestCase):
    def _base_signal(self) -> dict:
        return {
            "time_msk": "04.04.2026, 12:00",
            "symbol": "BTC/USDT",
            "price": 100.0,
            "direction": "long",
            "side": "long",
            "mode": "neutral",
            "entry_mode": "limit",
            "entry_range": {"min": 99.0, "max": 100.0},
            "sl": 96.0,
            "tp1": 103.0,
            "tp2": 106.0,
            "sl_by_mode": {"neutral": 96.0},
            "tp_by_mode": {"neutral": {"tvh1": 103.0, "tvh2": 106.0}},
            "rr_by_mode": {"neutral": 2.0},
            "confidence": "High",
            "warnings": [],
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "upcoming_events": [],
            "macro_risk_summary": "",
            "event_risk_context": [],
            "day_mid_context": {
                "day_bias": "long",
                "mid_bias": "long",
                "notes": "DAY/MID context",
                "upcoming_events": [],
                "macro_risk_summary": "",
                "event_risk_context": [],
            },
        }

    def test_high_impact_geopolitical_context_forces_wait_confirm_without_changing_direction(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("direction"), "long")
        self.assertEqual(d.get("side"), "long")
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual(d.get("confidence"), "Low")
        self.assertIn("event_risk_wait_confirm_preferred", d.get("warnings") or [])
        self.assertIn("event_risk_geopolitical_execution_caution", d.get("warnings") or [])
        self.assertIn("event_risk_no_chasing", d.get("warnings") or [])
        self.assertIn("event_risk_continuation_stricter", d.get("warnings") or [])
        self.assertIn("ретеста", str(d.get("confirmation_rules") or "").lower())
        self.assertEqual((d.get("event_risk") or {}).get("event_risk_level"), "severe")
        self.assertEqual((d.get("event_risk") or {}).get("event_bias"), "risk_off")
        self.assertEqual(
            d.get("event_risk_summary"),
            {
                "dominant_driver": "geopolitics",
                "max_impact": "high",
                "dominant_phase": "pre_event",
                "volatility_risk": "high",
                "execution_caution": "high",
            },
        )

    def test_high_impact_geopolitical_context_keeps_structural_no_trade_primary(self) -> None:
        d = self._base_signal()
        d["no_trade"] = True
        d["no_trade_reasons"] = ["counter_trend_neutral_forbidden"]
        d["no_trade_hint"] = "counter_trend_neutral_forbidden"
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertTrue(bool(d.get("no_trade")))
        self.assertEqual((d.get("no_trade_reasons") or [None])[0], "counter_trend_neutral_forbidden")
        self.assertNotIn("upcoming_high_impact_event_neutral_block", d.get("no_trade_reasons") or [])
        self.assertIn("event_risk_geopolitical_execution_caution", d.get("warnings") or [])
        self.assertIn(
            "вторичен",
            " ".join((d.get("event_risk") or {}).get("display_lines") or []).lower(),
        )

    def test_post_event_but_still_volatile_catalyst_keeps_caution_elevated(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz blockade aftermath keeps shipping risk elevated",
                "category": "geopolitics",
                "phase": "post_event",
                "impact": "high",
                "directional_risk": "risk_off",
                "summary": "Risk premium remains elevated and follow-through is unstable.",
                "drivers": ["shipping disruption keeps volatility elevated"],
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(
            d.get("event_risk_summary"),
            {
                "dominant_driver": "geopolitics",
                "max_impact": "high",
                "dominant_phase": "post_event",
                "volatility_risk": "high",
                "execution_caution": "high",
            },
        )
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertIn("event_risk_post_event_unstable_follow_through", d.get("warnings") or [])
        self.assertEqual((d.get("event_risk") or {}).get("event_risk_level"), "severe")
        self.assertEqual((d.get("event_risk") or {}).get("event_bias"), "risk_off")
        display_lines = " ".join((d.get("event_risk") or {}).get("display_lines") or []).lower()
        self.assertIn("тяжёлый геополитический режим", display_lines)
        self.assertIn("fragile geopolitical regime", display_lines)

    def test_no_strong_event_risk_preserves_existing_signal_behavior(self) -> None:
        d = self._base_signal()

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(d.get("entry_mode"), "limit")
        self.assertEqual(d.get("confidence"), "High")
        self.assertEqual(d.get("warnings"), [])
        self.assertEqual(d.get("macro_risk_summary"), "")
        self.assertEqual(
            d.get("event_risk_summary"),
            {
                "dominant_driver": "none",
                "max_impact": "none",
                "dominant_phase": "none",
                "volatility_risk": "low",
                "execution_caution": "low",
            },
        )
        self.assertIsNone(d.get("event_risk"))

    def test_macro_risk_summary_includes_dominant_non_calendar_catalyst(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "04.04.2026",
                "time_msk": "18:00",
                "event": "US CPI",
                "impact": "high",
                "category": "macro",
                "window_before_min": 30,
                "window_after_min": 60,
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = "04.04.2026, 18:00 — US CPI (high)"

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertIn("тяжёлый геополитический режим остаётся нерешённым", d.get("macro_risk_summary") or "")
        self.assertIn("US CPI", d.get("macro_risk_summary") or "")

    def test_macro_risk_summary_puts_structured_driver_first_and_calendar_second(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "04.04.2026",
                "time_msk": "18:00",
                "event": "US CPI",
                "impact": "high",
                "category": "macro",
                "window_before_min": 30,
                "window_after_min": 60,
            }
        ]
        d["day_mid_context"]["macro_risk_summary"] = (
            "US CPI can spike volatility into the close. "
            "геополитическая траектория эскалации остаётся нерешённой; рынок по-прежнему живёт заголовками."
        )

        get_signal_json.apply_upcoming_event_risk(d)

        summary = d.get("macro_risk_summary") or ""
        structured_text = "тяжёлый геополитический режим остаётся нерешённым"
        calendar_text = "04.04.2026, 18:00 — US CPI (high)"
        self.assertTrue(summary.startswith(structured_text))
        self.assertLess(summary.index(structured_text), summary.index(calendar_text))
        self.assertIn("04.04.2026, 18:00 — US CPI (high)", summary)
        self.assertEqual(summary.count(structured_text), 1)
        self.assertEqual(summary.count("US CPI"), 1)

    def test_structured_wait_confirm_warning_is_suppressed_when_calendar_wait_confirm_exists(self) -> None:
        d = self._base_signal()
        d["mode"] = "aggressive"
        d["entry_mode"] = "market"
        d["time_msk"] = "04.04.2026, 17:45"
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]
        d["day_mid_context"]["upcoming_events"] = [
            {
                "date_msk": "04.04.2026",
                "time_msk": "18:00",
                "event": "US CPI",
                "impact": "high",
                "category": "macro",
                "window_before_min": 30,
                "window_after_min": 60,
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        warnings = d.get("warnings") or []
        self.assertIn("upcoming_high_impact_event_wait_confirm", warnings)
        self.assertIn("event_risk_geopolitical_execution_caution", warnings)
        self.assertNotIn("event_risk_wait_confirm_preferred", warnings)
        self.assertNotIn("event_risk_no_chasing", warnings)

    def test_structured_only_event_risk_omits_empty_active_events(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        event_risk = d.get("event_risk") or {}
        self.assertNotIn("active_events", event_risk)
        self.assertIn("signal_summary", event_risk)

    def test_event_risk_does_not_mutate_direction_or_price_levels(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Hormuz escalation risk remains unresolved",
                "category": "geopolitics",
                "phase": "pre_event",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": "US-Iran talks failed, but blockade risk is not yet confirmed.",
                "drivers": ["headline sensitivity is elevated before clarity"],
            }
        ]
        before = copy.deepcopy(
            {
                "direction": d.get("direction"),
                "side": d.get("side"),
                "entry_range": d.get("entry_range"),
                "sl": d.get("sl"),
                "tp1": d.get("tp1"),
                "tp2": d.get("tp2"),
                "sl_by_mode": d.get("sl_by_mode"),
                "tp_by_mode": d.get("tp_by_mode"),
            }
        )

        get_signal_json.apply_upcoming_event_risk(d)

        after = {
            "direction": d.get("direction"),
            "side": d.get("side"),
            "entry_range": d.get("entry_range"),
            "sl": d.get("sl"),
            "tp1": d.get("tp1"),
            "tp2": d.get("tp2"),
            "sl_by_mode": d.get("sl_by_mode"),
            "tp_by_mode": d.get("tp_by_mode"),
        }
        self.assertEqual(after, before)

    def test_severe_unscheduled_geopolitical_bundle_dominates_empty_calendar_and_hardens_signal_framing(self) -> None:
        d = self._base_signal()
        d["day_mid_context"]["event_risk_context"] = [
            {
                "event": "Fragile ceasefire and renewed military threat keep negotiations unresolved",
                "category": "geopolitics",
                "phase": "ongoing",
                "impact": "high",
                "directional_risk": "uncertain",
                "summary": (
                    "Negotiations are ongoing, but ceasefire collapse risk increased and renewed military action "
                    "remains possible."
                ),
                "drivers": [
                    "headline sensitivity stays elevated while tail risk is repriced",
                    "public threats keep escalation probability elevated",
                ],
                "confirmed_facts": ["Negotiations are ongoing"],
                "anticipated_consequences": [
                    "Ceasefire collapse risk increased",
                    "Renewed military action risk increased",
                ],
            }
        ]

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertEqual(d.get("upcoming_events"), [])
        self.assertEqual((d.get("event_risk_regime") or {}).get("severity"), "severe")
        self.assertEqual((d.get("event_risk_regime") or {}).get("driver"), "geopolitics")
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual(d.get("confidence"), "Low")
        self.assertIn(
            "пустой scheduled calendar не снижает уязвимость к внеплановым заголовкам",
            (d.get("macro_risk_summary") or "").lower(),
        )
        display_lines = " ".join((d.get("event_risk") or {}).get("display_lines") or []).lower()
        self.assertIn("только тактически", display_lines)
        self.assertIn("buy-the-dip", display_lines)
        warnings = d.get("warnings") or []
        self.assertIn("event_risk_geopolitical_regime_severe", warnings)
        self.assertIn("event_risk_downside_shock_asymmetry", warnings)
        self.assertIn("event_risk_tactical_only_continuation", warnings)
        self.assertNotIn("Iran", d.get("macro_risk_summary") or "")
        self.assertNotIn("Hormuz", d.get("macro_risk_summary") or "")

    def test_fragile_geopolitical_regime_converts_severe_dirty_aggressive_continuation_to_urgent_soft_veto(self) -> None:
        d = self._base_signal()
        d["mode"] = "aggressive"
        d["entry_mode"] = "market"
        d["confidence"] = "Medium"
        d["rr_by_mode"] = {"aggressive": 1.7}
        d["day_mid_context"]["event_risk_context"] = build_event_risk_context(
            "\n".join(
                [
                    "- [2026-04-12 08:00 МСК] [impact:neutral] Border ceasefire remains fragile as diplomats warn the truce may collapse",
                    "- [2026-04-12 08:10 МСК] [impact:−] Negotiations continue without agreement while officials threaten renewed military action",
                    "- [2026-04-12 08:20 МСК] [impact:−] Public statements raise escalation risk and keep downside shock sensitivity elevated",
                ]
            ),
            calendar_events=[],
        ).get("event_risk_context") or []

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertTrue(bool(d.get("urgent_flag")))
        self.assertEqual(d.get("soft_veto_reason"), "fragile_geopolitics_aggressive_continuation_forbidden")
        self.assertIn("жёсткого подтверждения", d.get("urgent_message") or "")
        self.assertEqual((d.get("event_risk") or {}).get("mode_action"), "wait_confirm")
        self.assertTrue(bool((d.get("event_risk") or {}).get("urgent_flag")))
        self.assertEqual(
            (d.get("event_risk") or {}).get("soft_veto_reason"),
            "fragile_geopolitics_aggressive_continuation_forbidden",
        )
        self.assertIn("event_risk_aggressive_continuation_downgrade_pressure", d.get("warnings") or [])
        self.assertIn("event_risk_fragile_regime_reject_marginal_aggressive", d.get("warnings") or [])
        self.assertIn("event_risk_fragile_regime_urgent_soft_veto_aggressive", d.get("warnings") or [])
        self.assertEqual(d.get("no_trade_reasons"), [])

    def test_fragile_geopolitical_regime_keeps_other_hard_veto_paths_for_neutral_marginal_setup(self) -> None:
        d = self._base_signal()
        d["mode"] = "neutral"
        d["entry_mode"] = "market"
        d["confidence"] = "Medium"
        d["rr_by_mode"] = {"neutral": 1.7}
        d["day_mid_context"]["event_risk_context"] = build_event_risk_context(
            "\n".join(
                [
                    "- [2026-04-12 08:00 МСК] [impact:neutral] Border ceasefire remains fragile as diplomats warn the truce may collapse",
                    "- [2026-04-12 08:10 МСК] [impact:−] Negotiations continue without agreement while officials threaten renewed military action",
                    "- [2026-04-12 08:20 МСК] [impact:−] Public statements raise escalation risk and keep downside shock sensitivity elevated",
                ]
            ),
            calendar_events=[],
        ).get("event_risk_context") or []

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertTrue(bool(d.get("no_trade")))
        self.assertIn("fragile_geopolitics_neutral_marginal_rejected", d.get("no_trade_reasons") or [])
        self.assertEqual((d.get("event_risk") or {}).get("mode_action"), "no_trade")
        self.assertFalse(bool(d.get("urgent_flag")))

    def test_fragile_geopolitical_regime_keeps_clean_aggressive_setup_tactical_not_blanket_blocked(self) -> None:
        d = self._base_signal()
        d["mode"] = "aggressive"
        d["entry_mode"] = "market"
        d["confidence"] = "Medium"
        d["rr_by_mode"] = {"aggressive": 2.2}
        d["price_vs_ema20_h1"] = "above"
        d["ema_fan_h1_state"] = "bull"
        d["ema_fan_m15_state"] = "bull"
        d["day_mid_context"]["event_risk_context"] = build_event_risk_context(
            "\n".join(
                [
                    "- [2026-04-12 08:00 МСК] [impact:neutral] Border ceasefire remains fragile as diplomats warn the truce may collapse",
                    "- [2026-04-12 08:10 МСК] [impact:−] Negotiations continue without agreement while officials threaten renewed military action",
                    "- [2026-04-12 08:20 МСК] [impact:−] Public statements raise escalation risk and keep downside shock sensitivity elevated",
                ]
            ),
            calendar_events=[],
        ).get("event_risk_context") or []

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual((d.get("event_risk") or {}).get("mode_action"), "wait_confirm")
        self.assertIn("event_risk_aggressive_continuation_downgrade_pressure", d.get("warnings") or [])
        self.assertIn("event_risk_fragile_regime_prefer_neutral_aggressive", d.get("warnings") or [])
        self.assertNotIn("fragile_geopolitics_aggressive_continuation_forbidden", d.get("no_trade_reasons") or [])

    def test_fragile_geopolitical_regime_keeps_non_severe_dirty_aggressive_continuation_on_downgrade_path(self) -> None:
        d = self._base_signal()
        d["mode"] = "aggressive"
        d["entry_mode"] = "market"
        d["confidence"] = "Medium"
        d["rr_by_mode"] = {"aggressive": 1.7}
        d["day_mid_context"]["event_risk_context"] = build_event_risk_context(
            "\n".join(
                [
                    "- [2026-04-12 08:00 МСК] [impact:neutral] Border ceasefire remains fragile while diplomats keep emergency talks open",
                    "- [2026-04-12 08:10 МСК] [impact:neutral] Officials say the truce still lacks durable guarantees and headline sensitivity remains elevated",
                ]
            ),
            calendar_events=[],
        ).get("event_risk_context") or []

        get_signal_json.apply_upcoming_event_risk(d)

        self.assertFalse(bool(d.get("no_trade")))
        self.assertEqual(d.get("entry_mode"), "wait_confirm")
        self.assertEqual((d.get("event_risk") or {}).get("mode_action"), "wait_confirm")
        self.assertIn("event_risk_aggressive_continuation_downgrade_pressure", d.get("warnings") or [])
        self.assertIn("event_risk_fragile_regime_prefer_neutral_aggressive", d.get("warnings") or [])
        self.assertNotIn("fragile_geopolitics_aggressive_continuation_forbidden", d.get("no_trade_reasons") or [])


if __name__ == "__main__":
    unittest.main()
