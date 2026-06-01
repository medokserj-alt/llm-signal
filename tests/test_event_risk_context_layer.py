import unittest

import get_signal_json
from event_risk_context import build_event_risk_context, render_event_risk_context_section


class TestEventRiskContextLayer(unittest.TestCase):
    def test_critical_topic_detects_iran_talks_halted_as_high_headline_risk(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-06-01 10:00 МСК] [impact:neutral] Iran halts talks with US after latest escalation",
            calendar_events=[],
        )

        self.assertTrue(snapshot.get("headline_risk_active"))
        self.assertEqual(snapshot.get("event_risk_level"), "high")
        self.assertEqual(snapshot.get("event_bias"), "risk_off")
        self.assertEqual((snapshot.get("dominant_critical_topic") or {}).get("topic_id"), "geopolitical_military_escalation")

    def test_critical_topic_detects_hormuz_blockade_as_severe(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-06-01 10:00 МСК] [impact:neutral] Iran threatens to block Strait of Hormuz as oil tankers come under fire",
            calendar_events=[],
        )

        self.assertEqual(snapshot.get("event_risk_level"), "severe")
        self.assertEqual((snapshot.get("dominant_critical_topic") or {}).get("topic_id"), "strategic_shipping_energy_chokepoint")
        self.assertEqual(snapshot.get("confirm_policy"), "block_stale_confirm")

    def test_critical_topic_preserves_multiple_topics_and_dominant(self) -> None:
        news = "\n".join(
            [
                "- US says it struck Iranian radar sites as Kuwait reports missile and drone attacks",
                "- ETF outflows hit a record while Strategy sells BTC into weakness",
            ]
        )
        snapshot = build_event_risk_context(news, calendar_events=[])
        topic_ids = [topic.get("topic_id") for topic in snapshot.get("critical_topics") or []]

        self.assertIn("geopolitical_military_escalation", topic_ids)
        self.assertIn("crypto_market_structure_shock", topic_ids)
        self.assertTrue(snapshot.get("dominant_critical_topic"))

    def test_stablecoin_adoption_does_not_trigger_critical_macro_policy(self) -> None:
        snapshot = build_event_risk_context(
            "- Stablecoin adoption expands in Brazil payments as transaction volume grows",
            calendar_events=[],
        )

        self.assertFalse(snapshot.get("headline_risk_active"))
        self.assertEqual(snapshot.get("critical_topics"), [])

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
        self.assertIn("Переговоры продолжаются", items[0]["summary"])

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
        self.assertIn("повышенную премию за риск", items[0]["summary"])
        self.assertIn("волатильность на заголовках", items[0]["summary"])
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
        self.assertIn("US-Iran talks failed, but Hormuz blockade пока не подтверждён.", item["summary"])
        self.assertIn("волатильность остаются повышенными", item["summary"])
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

    def test_local_hospital_assault_is_filtered_out_of_event_risk(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-26 09:15 МСК] [impact:−] Patient allegedly attacks several nurses, police and member of the public at Sydney hospital",
            calendar_events=[],
        )

        self.assertEqual(snapshot.get("event_risk_context"), [])
        self.assertEqual(render_event_risk_context_section(snapshot, profile="day"), "")

    def test_crypto_etf_and_exchange_hack_headlines_remain_market_relevant(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-26 09:00 МСК] [impact:+] Spot Bitcoin ETF approval odds rise after SEC custody talks",
                "- [2026-04-26 09:05 МСК] [impact:−] Major crypto exchange hack forces temporary withdrawal halt",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        items = snapshot.get("event_risk_context") or []

        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["category"] == "crypto_market_structure" for item in items))

    def test_stablecoin_adoption_headline_is_not_high_macro_policy_shock(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-26 09:00 МСК] [impact:neutral] Central Bank of Brazil: Stablecoins Dominate Over $6.9 Billion Crypto Purchases Registered in Q1",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertEqual(item["category"], "crypto_market_structure")
        self.assertEqual(item["impact"], "medium")
        self.assertEqual(item["directional_risk"], "mixed")
        self.assertIn("рост использования стейблкоинов", item["summary"])
        self.assertIn("тему крипто-ликвидности", item["summary"])
        self.assertNotIn("немедленный риск исполнения", item["summary"])
        self.assertNotIn("принудительные потоки", item["summary"])

        rendered = render_event_risk_context_section(snapshot, profile="day")
        self.assertIn("рост использования стейблкоинов", rendered)
        self.assertNotIn("принудительные потоки", rendered)

    def test_day_renders_event_risk_block_when_context_exists(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:00 МСК] [impact:−] Exchange outage halts withdrawals as liquidation cascade accelerates",
            calendar_events=[],
        )

        rendered = render_event_risk_context_section(snapshot, profile="day")

        self.assertIn("⚡ Катализаторы событийного риска", rendered)
        self.assertIn("crypto_market_structure", rendered)
        self.assertIn("Риск исполнения:", rendered)

    def test_mid_renders_event_risk_block_when_context_exists(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-12 12:00 МСК] [impact:neutral] U.S.-Iran talks expected later this weekend in Muscat",
            calendar_events=[],
        )

        rendered = render_event_risk_context_section(snapshot, profile="mid")

        self.assertIn("⚡ Катализаторы смены режима", rendered)
        self.assertIn("geopolitics", rendered)
        self.assertIn("Режим:", rendered)

    def test_white_house_ceasefire_talks_are_pre_event_and_not_merged_into_gulf_chain(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-23 18:13 МСК] [impact:neutral] Lebanon and Israel to resume rare direct talks in Washington to extend Israel-Hezbollah ceasefire",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertEqual(item["category"], "geopolitics")
        self.assertEqual(item["phase"], "pre_event")
        self.assertEqual(item["directional_risk"], "uncertain")
        self.assertIn("Israel-Lebanon", item["event"])
        self.assertNotIn("|gulf|", item.get("cluster_key") or "")
        self.assertNotEqual(item.get("cluster_key"), "geopolitics|gulf|escalation_chain")

    def test_ceasefire_extension_is_risk_on_and_stays_outside_hormuz_cluster(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-04-24 03:45 МСК] [impact:neutral] Lebanon-Israel ceasefire extended by 3 weeks after Oval Office meeting",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]

        self.assertEqual(item["category"], "geopolitics")
        self.assertEqual(item["phase"], "post_event")
        self.assertEqual(item["directional_risk"], "risk_on")
        self.assertIn("ceasefire was extended", " ".join(item.get("confirmed_facts") or []).lower())
        self.assertNotIn("|gulf|", item.get("cluster_key") or "")
        self.assertNotEqual(item.get("cluster_key"), "geopolitics|gulf|escalation_chain")

    def test_ceasefire_life_support_and_rejected_response_raise_fragile_regime(self) -> None:
        snapshot = build_event_risk_context(
            "- [2026-05-11 22:14 МСК] [impact:neutral] Trump says ceasefire is on massive life support after rejecting Iran’s response to US peace proposal",
            calendar_events=[],
        )
        item = (snapshot.get("event_risk_context") or [])[0]
        regime_layer = snapshot.get("regime_layer") or {}

        self.assertEqual(item["category"], "geopolitics")
        self.assertEqual(item["phase"], "pre_event")
        self.assertEqual(item["impact"], "high")
        self.assertEqual(item["directional_risk"], "uncertain")
        self.assertIn("Ceasefire collapse risk increased", item.get("anticipated_consequences") or [])
        self.assertIn("ceasefire_at_risk", item.get("regime_flags") or [])
        self.assertIn("diplomatic_breakdown_risk", item.get("regime_flags") or [])
        self.assertIn(item.get("regime_severity"), {"high", "severe"})
        self.assertIn(item.get("continuation_mode"), {"confirmation_first", "tactical_only"})
        self.assertTrue(
            {"fragile_regime", "continuation_unstable"} & set(item.get("regime_flags") or [])
        )
        self.assertEqual(regime_layer.get("driver"), "geopolitics")
        self.assertIn(regime_layer.get("severity"), {"high", "severe"})

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
        self.assertIn("US-Iran talks failed, but Hormuz blockade пока не подтверждён.", rendered)
        self.assertIn("Риск исполнения: один компонент уже подтверждён", rendered)
        self.assertIn("следующий шаг эскалации остаётся нерешённым", rendered)
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

    def test_generic_severe_geopolitical_bundle_elevates_regime_layer_and_hardens_rendering(self) -> None:
        news = "\n".join(
            [
                "- [2026-04-12 08:00 МСК] [impact:neutral] Border ceasefire remains fragile as diplomats warn the truce may collapse",
                "- [2026-04-12 08:10 МСК] [impact:−] Negotiations continue without agreement while officials threaten renewed military action",
                "- [2026-04-12 08:20 МСК] [impact:−] Public statements raise escalation risk and keep downside shock sensitivity elevated",
            ]
        )

        snapshot = build_event_risk_context(news, calendar_events=[])
        regime_layer = snapshot.get("regime_layer") or {}
        item = (snapshot.get("event_risk_context") or [])[0]
        rendered_day = render_event_risk_context_section(snapshot, profile="day")
        rendered_mid = render_event_risk_context_section(snapshot, profile="mid")

        self.assertEqual(regime_layer.get("driver"), "geopolitics")
        self.assertEqual(regime_layer.get("severity"), "severe")
        self.assertIn("ceasefire_at_risk", regime_layer.get("flags") or [])
        self.assertIn("unresolved_high_stakes_negotiation", regime_layer.get("flags") or [])
        self.assertIn("unresolved_geopolitical_breakpoint", regime_layer.get("flags") or [])
        item_flags = {
            flag
            for event_item in snapshot.get("event_risk_context") or []
            for flag in event_item.get("regime_flags") or []
        }
        self.assertIn("diplomatic_breakdown_risk", item_flags)
        self.assertIn("renewed_military_action_threat", item_flags)
        self.assertIn(item.get("regime_severity"), {"high", "severe"})
        self.assertIn("asymmetric downside shock risk", regime_layer.get("summary") or "")
        self.assertIn("Слой режима [severe | geopolitics]", rendered_day)
        self.assertIn("asymmetric downside shock risk", rendered_day)
        self.assertIn("tactical-only", rendered_day)
        self.assertIn("tactical-only", rendered_mid)
        self.assertNotIn("Iran", rendered_day)
        self.assertNotIn("Hormuz", rendered_day)


if __name__ == "__main__":
    unittest.main()
