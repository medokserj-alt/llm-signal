from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import get_signal_json
import scheduled_runner
from macro_event_guard import evaluate_macro_event_guard, normalize_scheduled_macro_events


def _cpi_event() -> dict:
    return {
        "time_msk": "10.06.2026, 15:30",
        "date_msk": "10.06.2026",
        "event": "US CPI",
        "category": "macro",
        "impact": "high",
        "window_before_min": 90,
        "source": "calendar",
    }


class TestScheduledMacroEventGuard(unittest.TestCase):
    def test_day_mid_structured_scheduled_event_persisted(self) -> None:
        payload = {"upcoming_events": [], "macro_risk_summary": "", "day_mid_context": {}}
        get_signal_json.merge_event_calendar_context(
            payload,
            profile="day",
            now_msk="10.06.2026, 09:00",
            calendar_context={"generated_at_utc": "2026-06-10T06:00:00Z", "calendar_events": [_cpi_event()]},
        )

        event = payload["scheduled_macro_events"][0]
        self.assertEqual(event["event_id"], "us_cpi_20260610_1530")
        self.assertEqual(event["event_name"], "US CPI")
        self.assertEqual(event["category"], "macro_inflation")
        self.assertEqual(event["impact"], "high")
        self.assertEqual(event["pre_blackout_minutes"], 90)
        self.assertIs(event["post_analysis_required"], True)
        self.assertEqual(event["policy_pre_event"], "no_new_entries")
        self.assertEqual(event["policy_at_event"], "macro_event_update_required")
        self.assertEqual(event["policy_post_event"], "require_reprice_analysis")
        self.assertIs(event["substitute_scheduled_signal"], True)

    def test_pre_event_blackout_blocks_new_directional_signal(self) -> None:
        signal = {
            "time_msk": "10.06.2026, 14:00",
            "symbol": "BTC/USDT",
            "direction": "short",
            "side": "short",
            "mode": "aggressive",
            "no_trade": False,
            "no_trade_reasons": [],
            "upcoming_events": [_cpi_event()],
            "day_mid_context": {},
        }

        with mock.patch.dict("os.environ", {}, clear=False):
            get_signal_json.apply_scheduled_macro_event_guard(signal)

        self.assertIs(signal["no_trade"], True)
        self.assertIn("scheduled_macro_pre_event_blackout", signal["no_trade_reasons"])
        self.assertEqual(signal["macro_event_guard"]["phase"], "pre_event")
        self.assertEqual(signal["blackout_remaining_minutes"], 90)

    def test_post_event_without_classification_requires_reprice(self) -> None:
        guard = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 12, 31, tzinfo=timezone.utc),
            events=normalize_scheduled_macro_events([_cpi_event()]),
            side="short",
        )

        self.assertIs(guard["blocked"], True)
        self.assertEqual(guard["reason"], "post_event_reprice_required")
        self.assertEqual(guard["macro_message_type"], "MACRO_EVENT_ANALYSIS_PENDING")

    def test_missing_post_event_classification_ttl_expires_stale_cpi(self) -> None:
        events = normalize_scheduled_macro_events([_cpi_event()])

        pre_event = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 11, 30, tzinfo=timezone.utc),
            events=events,
            side="short",
        )
        self.assertIs(pre_event["active"], True)
        self.assertIs(pre_event["blocked"], True)
        self.assertEqual(pre_event["phase"], "pre_event")
        self.assertEqual(pre_event["reason"], "scheduled_macro_pre_event_blackout")

        pending_1535 = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 12, 35, tzinfo=timezone.utc),
            events=events,
            side="short",
        )
        self.assertIs(pending_1535["active"], True)
        self.assertIs(pending_1535["blocked"], True)
        self.assertEqual(pending_1535["phase"], "awaiting_reprice")
        self.assertEqual(pending_1535["reason"], "post_event_reprice_required")
        self.assertEqual(pending_1535["macro_message_type"], "MACRO_EVENT_ANALYSIS_PENDING")

        pending_1600 = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
            events=events,
            side="short",
        )
        self.assertIs(pending_1600["active"], True)
        self.assertEqual(pending_1600["reason"], "post_event_reprice_required")

        expired_1831 = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 15, 31, tzinfo=timezone.utc),
            events=events,
            side="short",
        )
        self.assertIs(expired_1831["active"], False)
        self.assertIs(expired_1831["blocked"], False)
        self.assertEqual(expired_1831["phase"], "expired_missing_classification")
        self.assertEqual(expired_1831["reason"], "macro_event_reprice_expired_without_classification")

    def test_softer_risk_on_classification_invalidates_old_btc_short(self) -> None:
        event = _cpi_event()
        event["post_event_classification"] = {
            "event_name": "US CPI",
            "event_time_msk": "10.06.2026, 15:30",
            "actual_vs_forecast": "softer",
            "core_actual_vs_forecast": "softer",
            "market_reaction": "risk_on",
            "btc_reaction": "impulse_up",
            "old_narrative_valid": False,
            "allowed_direction": "long",
            "execution_policy": "no_chase_wait_pullback",
            "generated_at_utc": "2026-06-10T12:45:00Z",
            "source": "test",
        }

        short_guard = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 12, 45, tzinfo=timezone.utc),
            events=normalize_scheduled_macro_events([event]),
            side="short",
        )
        long_guard = evaluate_macro_event_guard(
            now=datetime(2026, 6, 10, 12, 45, tzinfo=timezone.utc),
            events=normalize_scheduled_macro_events([event]),
            side="long",
        )

        self.assertIs(short_guard["blocked"], True)
        self.assertEqual(short_guard["reason"], "macro_event_direction_requires_fresh_breakdown")
        self.assertIs(long_guard["blocked"], False)
        self.assertEqual(long_guard["reason"], "post_event_no_chase_wait_retest")

    def test_chaotic_classification_blocks_directional_signal(self) -> None:
        event = _cpi_event()
        event["post_event_classification"] = {
            "market_reaction": "chaotic",
            "btc_reaction": "mixed",
            "old_narrative_valid": False,
            "allowed_direction": "none",
            "execution_policy": "no_trade_chaotic",
        }
        signal = {
            "time_msk": "10.06.2026, 15:45",
            "symbol": "BTC/USDT",
            "direction": "long",
            "side": "long",
            "mode": "aggressive",
            "no_trade": False,
            "no_trade_reasons": [],
            "upcoming_events": [event],
            "day_mid_context": {},
        }

        get_signal_json.apply_scheduled_macro_event_guard(signal)

        self.assertIs(signal["no_trade"], True)
        self.assertIn("post_event_reaction_chaotic", signal["no_trade_reasons"])

    def test_debug_override_allows_manual_blackout_signal(self) -> None:
        signal = {
            "time_msk": "10.06.2026, 14:30",
            "symbol": "BTC/USDT",
            "direction": "short",
            "side": "short",
            "mode": "aggressive",
            "no_trade": False,
            "no_trade_reasons": [],
            "upcoming_events": [_cpi_event()],
            "day_mid_context": {},
        }

        with mock.patch.dict("os.environ", {"ALLOW_MACRO_BLACKOUT_SIGNAL": "1"}):
            get_signal_json.apply_scheduled_macro_event_guard(signal)

        self.assertIs(signal["macro_event_guard"]["active"], True)
        self.assertIs(signal["macro_event_guard"]["blocked"], False)
        self.assertIs(signal["no_trade"], False)

    def test_scheduled_slot_at_cpi_is_macro_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "scheduled_signal_state.json"
            cfg = scheduled_runner.SchedulerConfig(
                start_date_msk=scheduled_runner.date(2026, 6, 10),
                target_chat_ids=[-1001],
                day_enabled=False,
                day_time_msk="09:00",
                mid_enabled=False,
                mid_time_msk="08:45",
                mid_interval_days=3,
                signal_enabled=True,
                signal_default_mode="aggressive",
                signal_slots_msk=["15:30"],
                retry_delay_minutes=60,
                max_attempts=2,
                gate_mode="soft",
                preferred_mode_downgrade_enabled=False,
                soft_avoid_downgrade=False,
                signal_state_path=state_path,
                publish_state_path=tmp_path / "publish.json",
                due_window_minutes=5,
            )
            now = datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc)
            with mock.patch.object(
                scheduled_runner,
                "load_scheduled_macro_events",
                lambda current: normalize_scheduled_macro_events([_cpi_event()], now=current),
            ), mock.patch.object(scheduled_runner, "load_aia_context", lambda: {}), mock.patch.object(
                scheduled_runner, "signal_decision_log_path", lambda current: tmp_path / "decisions.jsonl"
            ), mock.patch.object(
                scheduled_runner, "publish_macro_event_message", mock.AsyncMock(return_value=[1001])
            ):
                asyncio.run(
                    scheduled_runner.run_signal_slot(
                        now,
                        cfg,
                        {},
                        "20260610_1530",
                        scheduled_runner.slot_datetime_msk(scheduled_runner.date(2026, 6, 10), "15:30"),
                        1,
                        dry_run=False,
                    )
                )

            state = scheduled_runner.read_json(state_path)
            slot = state["slots"]["20260610_1530"]
            self.assertEqual(slot["status"], "macro_substitution")
            self.assertEqual(slot["reason"], "post_event_reprice_required")
            self.assertEqual(slot["macro_message_type"], "MACRO_EVENT_ANALYSIS_PENDING")

    def test_stale_cpi_next_day_0030_uses_normal_scheduled_signal_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "scheduled_signal_state.json"
            cfg = scheduled_runner.SchedulerConfig(
                start_date_msk=scheduled_runner.date(2026, 6, 10),
                target_chat_ids=[-1001],
                day_enabled=False,
                day_time_msk="09:00",
                mid_enabled=False,
                mid_time_msk="08:45",
                mid_interval_days=3,
                signal_enabled=True,
                signal_default_mode="aggressive",
                signal_slots_msk=["00:30"],
                retry_delay_minutes=60,
                max_attempts=2,
                gate_mode="soft",
                preferred_mode_downgrade_enabled=False,
                soft_avoid_downgrade=False,
                signal_state_path=state_path,
                publish_state_path=tmp_path / "publish.json",
                due_window_minutes=5,
            )
            now = datetime(2026, 6, 10, 21, 30, tzinfo=timezone.utc)
            publish_macro = mock.AsyncMock(return_value=[1001])
            generate_signal = mock.AsyncMock(return_value={"published": True, "reason": "published", "signal_id": "sig-0030"})
            with mock.patch.object(
                scheduled_runner,
                "load_scheduled_macro_events",
                lambda current: normalize_scheduled_macro_events([_cpi_event()], now=current),
            ), mock.patch.object(scheduled_runner, "load_aia_context", lambda: {}), mock.patch.object(
                scheduled_runner, "signal_decision_log_path", lambda current: tmp_path / "decisions.jsonl"
            ), mock.patch.object(
                scheduled_runner, "publish_macro_event_message", publish_macro
            ), mock.patch.object(
                scheduled_runner, "generate_and_publish_signal", generate_signal
            ):
                asyncio.run(
                    scheduled_runner.run_signal_slot(
                        now,
                        cfg,
                        {},
                        "20260611_0030",
                        scheduled_runner.slot_datetime_msk(scheduled_runner.date(2026, 6, 11), "00:30"),
                        1,
                        dry_run=False,
                    )
                )

            state = scheduled_runner.read_json(state_path)
            slot = state["slots"]["20260611_0030"]
            self.assertEqual(slot["status"], "published")
            self.assertEqual(slot["reason"], "published")
            self.assertEqual(slot["signal_id"], "sig-0030")
            publish_macro.assert_not_awaited()
            generate_signal.assert_awaited_once()

            decision = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(decision["decision"], "publish")
            self.assertEqual(decision["macro_phase"], "expired_missing_classification")
            self.assertEqual(decision["macro_reason"], "macro_event_reprice_expired_without_classification")


if __name__ == "__main__":
    unittest.main()
