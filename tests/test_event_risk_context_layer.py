import unittest

import get_signal_json
from event_risk_context import build_event_risk_context, render_event_risk_context_section


class TestEventRiskContextLayer(unittest.TestCase):
    def test_related_talks_headlines_cluster_into_one_ongoing_catalyst(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-12 10:00 МСК] [impact:neutral] U.S.-Iran talks expected later this weekend in Muscat as traders wait for clarity — https://example.test/1",
                "- [2026-04-12 11:00 МСК] [impact:neutral] U.S.-Iran talks continue in Muscat with ceasefire terms still unresolved — https://example.test/2",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        items = snapshot.get("event_risk_context") or []

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["phase"], "ongoing")
        self.assertEqual(items[0]["category"], "geopolitics")
        self.assertEqual(items[0]["directional_risk"], "uncertain")
        self.assertEqual(items[0]["time_text"], "ongoing")
        self.assertEqual(items[0]["confirmed_facts"], ["US-Iran talks are ongoing"])
        self.assertIn("Negotiations are ongoing", items[0]["summary"])

    def test_failed_talks_without_new_escalation_remain_post_event(self) -> None:
        news = (
            "- [2026-04-12 12:00 МСК] [impact:−] U.S.-Iran talks ended without agreement after the latest round"
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        items = snapshot.get("event_risk_context") or []

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["phase"], "post_event")
        self.assertEqual(items[0]["impact"], "medium")
        self.assertEqual(items[0]["directional_risk"], "risk_off")
        self.assertEqual(items[0]["time_text"], "recent")
        self.assertIn("risk premium elevated", items[0]["summary"])
        self.assertEqual(items[0]["confirmed_facts"], ["US-Iran talks failed"])

    def test_mixed_geopolitical_headline_keeps_unresolved_escalation_in_pre_event(self) -> None:
        title = "Trump announces naval blockade of Strait of Hormuz as Iran peace talks fail"

        snapshot = build_event_risk_context(
            f"- [2026-04-12 12:00 МСК] [impact:−] {title}",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertEqual(item["phase"], "pre_event")
        self.assertEqual(item["directional_risk"], "uncertain")
        self.assertEqual(item["time_text"], "recent")
        self.assertEqual(item["confirmed_facts"], ["US-Iran talks failed"])
        self.assertEqual(item["anticipated_consequences"], ["Hormuz blockade risk increased"])
        self.assertIn("Hormuz blockade risk increased", item["event"])
        self.assertIn("US-Iran talks failed, but Hormuz blockade is not yet confirmed.", item["summary"])
        self.assertNotEqual(item["summary"], title)

    def test_confirmed_escalation_headline_becomes_post_event(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:30 МСК] [impact:−] Strait of Hormuz blockade imposed after Iran talks collapse",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertEqual(item["phase"], "post_event")
        self.assertEqual(item["impact"], "high")
        self.assertEqual(item["directional_risk"], "risk_off")
        self.assertIn("Hormuz blockade was imposed", item["confirmed_facts"])

    def test_day_renders_event_risk_block_when_context_exists(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:00 МСК] [impact:−] Exchange outage halts withdrawals as liquidation cascade accelerates",
            calendar_events=[],
        )

        rendered = render_event_risk_context_section(snapshot, profile="day")

        self.assertIn("⚡ Event-risk catalysts", rendered)
        self.assertIn("crypto_market_structure", rendered)
        self.assertIn("Execution:", rendered)

    def test_mid_renders_event_risk_block_when_context_exists(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:00 МСК] [impact:neutral] U.S.-Iran talks expected later this weekend in Muscat",
            calendar_events=[],
        )

        rendered = render_event_risk_context_section(snapshot, profile="mid")

        self.assertIn("⚡ Regime-changing catalysts", rendered)
        self.assertIn("geopolitics", rendered)
        self.assertIn("Regime:", rendered)

    def test_no_fake_event_risk_block_when_no_relevant_event_exists(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:00 МСК] [impact:+] Bitcoin trades in a quiet range while volumes stay mixed",
            calendar_events=[],
        )

        self.assertEqual(snapshot.get("event_risk_context"), [])
        self.assertEqual(render_event_risk_context_section(snapshot, profile="day"), "")

    def test_scheduled_calendar_block_remains_separate(self) -> None:
        calendar_events = [
            {
                "time_msk": "13.04.2026, 15:30",
                "date_msk": "13.04.2026",
                "event": "US CPI",
                "category": "macro",
                "impact": "high",
                "window_before_min": 90,
                "window_after_min": 120,
                "note": "Macro release can reset volatility.",
            }
        ]
        snapshot = build_event_risk_context(
            "- [2026-04-12 09:00 МСК] [impact:neutral] US CPI due next session as traders wait for the print",
            calendar_events=calendar_events,
        )

        self.assertEqual(snapshot.get("event_risk_context"), [])
        rendered_calendar = get_signal_json.render_calendar_section(calendar_events)
        self.assertIn("🗓 Ключевые события периода", rendered_calendar)
        self.assertIn("US CPI", rendered_calendar)

    def test_related_hormuz_chain_headlines_collapse_into_one_cluster(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-12 09:00 МСК] [impact:neutral] U.S.-Iran talks expected later this weekend in Muscat",
                "- [2026-04-12 12:00 МСК] [impact:−] U.S.-Iran talks ended without agreement after the latest round",
                "- [2026-04-12 12:05 МСК] [impact:−] Trump announces naval blockade of Strait of Hormuz as Iran peace talks fail",
                "- [2026-04-12 12:10 МСК] [impact:−] Shipping insurers reprice Gulf routes as Hormuz blockade risk remains elevated",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        items = snapshot.get("event_risk_context") or []

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["phase"], "pre_event")
        self.assertEqual(item["time_text"], "recent")
        self.assertEqual(item["impact"], "high")
        self.assertEqual(item["confirmed_facts"], ["US-Iran talks failed"])
        self.assertEqual(len([text for text in item["anticipated_consequences"] if "blockade risk" in text.lower()]), 1)
        self.assertIn("Shipping disruption risk remains elevated", item["anticipated_consequences"])
        self.assertEqual(len(item["recent_developments"]), len(set(item["recent_developments"])))

    def test_event_risk_context_is_interpretive_not_raw_passthrough(self) -> None:
        title = "U.S.-Iran talks ended without agreement after the latest round"
        snapshot = build_event_risk_context(
            f"- [2026-04-12 16:00 МСК] [impact:−] {title}",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertNotEqual(item["summary"], title)
        self.assertGreaterEqual(len(item["drivers"]), 2)
        self.assertTrue(any("risk" in driver.lower() or "headline" in driver.lower() for driver in item["drivers"]))

    def test_rendered_mixed_summary_is_clean_and_grammatical(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:00 МСК] [impact:−] Trump announces naval blockade of Strait of Hormuz as Iran peace talks fail",
            calendar_events=[],
        )

        rendered = render_event_risk_context_section(snapshot, profile="day")

        self.assertIn("US-Iran talks failed; Hormuz blockade risk increased", rendered)
        self.assertIn("US-Iran talks failed, but Hormuz blockade is not yet confirmed.", rendered)
        self.assertIn("Execution: one component is confirmed, but the next escalation step is unresolved", rendered)
        self.assertNotIn("Market is in anticipation of Trump announces", rendered)
        self.assertEqual(rendered.count("Hormuz blockade risk increased"), 1)

    def test_major_geopolitical_catalyst_outranks_crypto_legal_noise(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-12 12:00 МСК] [impact:neutral] SEC lawsuit headline keeps crypto legal debate in focus",
                "- [2026-04-12 12:05 МСК] [impact:−] Trump announces naval blockade of Strait of Hormuz as Iran peace talks fail",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        items = snapshot.get("event_risk_context") or []

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["category"], "geopolitics")
        self.assertEqual(items[1]["category"], "crypto_market_structure")
        self.assertIn("Hormuz blockade risk increased", items[0]["event"])

    def test_confirmed_talks_failure_is_not_marked_expected_after_clustering(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-12 12:00 МСК] [impact:−] U.S.-Iran talks ended without agreement after the latest round",
                "- [2026-04-12 12:05 МСК] [impact:−] Strait of Hormuz blockade risk rises after talks collapse",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertEqual(item["phase"], "pre_event")
        self.assertEqual(item["time_text"], "recent")
        self.assertNotIn("(expected)", render_event_risk_context_section(snapshot, profile="day"))

    def test_event_risk_output_is_limited_and_market_wide_items_win(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-12 12:00 МСК] [impact:−] Trump announces naval blockade of Strait of Hormuz as Iran peace talks fail",
                "- [2026-04-12 12:01 МСК] [impact:−] Exchange outage halts withdrawals as liquidation cascade accelerates",
                "- [2026-04-12 12:02 МСК] [impact:neutral] SEC lawsuit headline keeps crypto legal debate in focus",
                "- [2026-04-12 12:03 МСК] [impact:−] Emergency tariff threat forces cross-asset repricing",
                "- [2026-04-12 12:04 МСК] [impact:neutral] BTC liquidation cluster sits above local highs",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        items = snapshot.get("event_risk_context") or []
        rendered_day = render_event_risk_context_section(snapshot, profile="day")
        rendered_mid = render_event_risk_context_section(snapshot, profile="mid")

        self.assertLessEqual(len(items), 3)
        self.assertEqual(rendered_day.count("\n- ["), 2)
        self.assertLessEqual(rendered_mid.count("\n- ["), 3)
        self.assertEqual(items[0]["category"], "geopolitics")

    def test_merge_day_mid_report_context_preserves_structured_event_risk_context(self) -> None:
        d = {
            "upcoming_events": [],
            "macro_risk_summary": "",
            "event_risk_context": [],
            "day_mid_context": {
                "day_bias": "long",
                "mid_bias": "long",
                "notes": "existing",
                "upcoming_events": [],
                "macro_risk_summary": "",
                "event_risk_context": [],
            },
        }
        day_report = {
            "event_risk_context": [
                {
                    "event": "U.S.-Iran talks expected later this weekend in Muscat",
                    "category": "geopolitics",
                    "phase": "pre_event",
                    "impact": "high",
                    "directional_risk": "uncertain",
                    "time_text": "expected",
                    "drivers": ["headline sensitivity is elevated before clarity"],
                    "summary": "Market is in anticipation of the talks outcome.",
                }
            ],
            "event_risk_context_timestamp_utc": "2026-04-12T09:00:00Z",
            "day_mid_context": {"notes": "DAY note"},
        }
        mid_report = {
            "event_risk_context": [
                {
                    "event": "U.S.-Iran talks ended without agreement",
                    "category": "geopolitics",
                    "phase": "post_event",
                    "impact": "high",
                    "directional_risk": "risk_off",
                    "time_text": "recent",
                    "drivers": ["geopolitical risk premium remains in play"],
                    "summary": "Failed talks keep risk premium elevated.",
                }
            ],
            "event_risk_context_timestamp_utc": "2026-04-12T16:00:00Z",
            "day_mid_context": {"notes": "MID note"},
        }

        get_signal_json.merge_day_mid_report_context(d, day_report=day_report, mid_report=mid_report)

        items = d.get("event_risk_context") or []
        self.assertEqual(len(items), 2)
        self.assertEqual([item["phase"] for item in items], ["pre_event", "post_event"])
        ctx = d.get("day_mid_context") or {}
        self.assertEqual([item["phase"] for item in ctx.get("event_risk_context") or []], ["pre_event", "post_event"])
        self.assertEqual(d.get("event_risk_context_timestamp_utc"), "2026-04-12T16:00:00Z")


if __name__ == "__main__":
    unittest.main()
