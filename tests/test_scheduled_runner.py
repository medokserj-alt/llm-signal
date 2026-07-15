import asyncio
import inspect
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scheduled_runner as sr


def cfg(tmp: Path | None = None) -> sr.SchedulerConfig:
    root = tmp or Path(tempfile.gettempdir())
    return sr.SchedulerConfig(
        start_date_msk=date(2026, 6, 5),
        target_chat_ids=[-1003492385200, -1003493070625, -1003530482991],
        day_enabled=True,
        day_time_msk="09:00",
        mid_enabled=True,
        mid_time_msk="08:45",
        mid_interval_days=3,
        signal_enabled=True,
        signal_default_mode="aggressive",
        signal_slots_msk=["09:30", "12:30", "15:30", "18:30", "21:30", "00:30"],
        retry_delay_minutes=60,
        max_attempts=2,
        gate_mode="soft",
        preferred_mode_downgrade_enabled=False,
        soft_avoid_downgrade=False,
        signal_state_path=root / "scheduled_signal_state.json",
        publish_state_path=root / "scheduled_publish_state.json",
        due_window_minutes=5,
    )


class TestScheduledTimeMath(unittest.TestCase):
    def test_day_due_at_0900_msk_starting_20260605(self) -> None:
        c = cfg()
        due, scheduled = sr.day_due(datetime(2026, 6, 5, 6, 0, tzinfo=timezone.utc), c)
        self.assertTrue(due)
        self.assertEqual(scheduled.isoformat(), "2026-06-05T09:00:00+03:00")

    def test_day_not_due_before_start_date(self) -> None:
        due, _ = sr.day_due(datetime(2026, 6, 4, 6, 0, tzinfo=timezone.utc), cfg())
        self.assertFalse(due)

    def test_mid_due_at_0845_and_repeats_every_three_days(self) -> None:
        c = cfg()
        due, scheduled, cycle = sr.mid_due(datetime(2026, 6, 5, 5, 45, tzinfo=timezone.utc), c)
        self.assertTrue(due)
        self.assertEqual(scheduled.isoformat(), "2026-06-05T08:45:00+03:00")
        self.assertEqual(cycle, "mid_20260605_0000")

        due, _, cycle = sr.mid_due(datetime(2026, 6, 8, 5, 45, tzinfo=timezone.utc), c)
        self.assertTrue(due)
        self.assertEqual(cycle, "mid_20260605_0001")

    def test_mid_not_due_on_non_cycle_days(self) -> None:
        due, _, _ = sr.mid_due(datetime(2026, 6, 6, 5, 45, tzinfo=timezone.utc), cfg())
        self.assertFalse(due)

    def test_signal_slots_due_and_msk_utc_conversion(self) -> None:
        c = cfg()
        due = sr.due_signal_slots(datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc), c, {"slots": {}})
        self.assertEqual(due[0][0], "20260605_0930")
        self.assertEqual(due[0][1].isoformat(), "2026-06-05T09:30:00+03:00")

        due = sr.due_signal_slots(datetime(2026, 6, 4, 21, 29, tzinfo=timezone.utc), c, {"slots": {}})
        self.assertFalse(due)

        due = sr.due_signal_slots(datetime(2026, 6, 5, 21, 30, tzinfo=timezone.utc), c, {"slots": {}})
        self.assertEqual(due[0][0], "20260606_0030")

    def test_deferred_slot_does_not_repeat_attempt_one_inside_due_window(self) -> None:
        c = cfg()
        state = {
            "slots": {
                "20260605_0930": {
                    "status": "deferred",
                    "slot_time_msk": "2026-06-05T09:30:00+03:00",
                    "attempt": 1,
                    "next_retry_at_msk": "2026-06-05T10:30:00+03:00",
                }
            }
        }
        due = sr.due_signal_slots(datetime(2026, 6, 5, 6, 31, tzinfo=timezone.utc), c, state)
        self.assertEqual(due, [])

        due = sr.due_signal_slots(datetime(2026, 6, 5, 7, 30, tzinfo=timezone.utc), c, state)
        self.assertEqual(due, [("20260605_0930", sr.slot_datetime_msk(date(2026, 6, 5), "09:30"), 2)])


class TestAiaGate(unittest.TestCase):
    def test_post_generation_severe_risk_off_long_without_flip_is_blocked(self) -> None:
        out = sr.evaluate_post_generation_event_risk_gate(
            {"direction": "long"},
            {"event_risk_level": "severe", "event_bias": "risk_off"},
        )
        self.assertFalse(out["can_publish_full_signal"])
        self.assertEqual(out["publication_type"], "blocked_by_severe_risk")
        self.assertEqual(out["execution_mode"], "BLOCKED")
        self.assertEqual(out["blocked_reason"], "counter_risk_without_regime_confirmation")

    def test_post_generation_background_severe_confirmed_major_long_is_tactical(self) -> None:
        out = sr.evaluate_post_generation_event_risk_gate(
            {"symbol": "BTC/USDT", "direction": "long", "entry_mode": "wait_confirm"},
            {"event_risk_level": "severe", "event_bias": "risk_off", "flow_bias": "bullish"},
        )
        self.assertTrue(out["can_publish_full_signal"])
        self.assertEqual(out["execution_mode"], "TACTICAL_CONFIRM_ONLY")
        self.assertEqual(out["publication_type"], "tactical_confirm_signal")
        self.assertEqual(out["entry_mode_required"], "WAIT_CONFIRM")
        self.assertEqual(out["risk_size_mode"], "REDUCED")

    def test_post_generation_severe_risk_off_short_is_allowed(self) -> None:
        out = sr.evaluate_post_generation_event_risk_gate(
            {"direction": "short"},
            {"event_risk_level": "severe", "event_bias": "risk_off"},
        )
        self.assertTrue(out["can_publish_full_signal"])

    def test_post_generation_severe_risk_off_long_with_flip_is_allowed(self) -> None:
        out = sr.evaluate_post_generation_event_risk_gate(
            {"direction": "long", "explicit_regime_flip_reason": "confirmed_market_reversal"},
            {"event_risk_level": "severe", "event_bias": "risk_off"},
        )
        self.assertTrue(out["can_publish_full_signal"])
        self.assertEqual(out["explicit_regime_flip_reason"], "confirmed_market_reversal")

    def test_immediate_fresh_escalation_still_blocks_counter_risk_long(self) -> None:
        out = sr.evaluate_post_generation_event_risk_gate(
            {"symbol": "BTC/USDT", "direction": "long", "entry_mode": "wait_confirm"},
            {"event_risk_level": "severe", "event_bias": "risk_off", "headline_risk_delta": "ESCALATION", "flow_bias": "bullish"},
        )
        self.assertFalse(out["can_publish_full_signal"])
        self.assertEqual(out["execution_mode"], "BLOCKED")
        self.assertEqual(out["blocked_reason"], "immediate_shock_counter_risk")

    def test_open_without_hard_block_allows_publish(self) -> None:
        out = sr.evaluate_aia_gate({"status": "OPEN", "preferred_mode": "aggressive"}, cfg())
        self.assertTrue(out["allowed"])
        self.assertEqual(out["selected_mode"], "aggressive")
        self.assertEqual(out["selected_mode_source"], "scheduled_default_soft_no_hard_block")

    def test_soft_avoid_without_hard_block_keeps_default_mode_when_downgrade_disabled(self) -> None:
        out = sr.evaluate_aia_gate(
            {"status": "AVOID", "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
            cfg(),
        )
        self.assertTrue(out["allowed"])
        self.assertTrue(out["aia_avoid_soft_allowed"])
        self.assertFalse(out["soft_avoid_downgrade"])
        self.assertFalse(out["preferred_mode_downgrade_enabled"])
        self.assertEqual(out["selected_mode"], "aggressive")
        self.assertEqual(out["selected_mode_source"], "scheduled_default_soft_no_hard_block")
        self.assertEqual(out["preferred_mode_ignored_reason"], "soft_gate_no_hard_block")
        self.assertEqual(out["reason"], "avoid_without_hard_block_keep_default_mode")

    def test_caution_directional_preferred_conservative_no_hard_block_keeps_default_mode(self) -> None:
        out = sr.evaluate_aia_gate(
            {"status": "CAUTION_DIRECTIONAL", "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
            cfg(),
        )
        self.assertTrue(out["allowed"])
        self.assertFalse(out["soft_avoid_downgrade"])
        self.assertEqual(out["selected_mode"], "aggressive")
        self.assertEqual(out["selected_mode_source"], "scheduled_default_soft_no_hard_block")
        self.assertEqual(out["preferred_mode_ignored_reason"], "soft_gate_no_hard_block")

    def test_avoid_hard_preferred_conservative_no_hard_block_keeps_default_mode(self) -> None:
        out = sr.evaluate_aia_gate(
            {"status": "AVOID_HARD", "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
            cfg(),
        )
        self.assertTrue(out["allowed"])
        self.assertFalse(out["soft_avoid_downgrade"])
        self.assertEqual(out["selected_mode"], "aggressive")
        self.assertEqual(out["selected_mode_source"], "scheduled_default_soft_no_hard_block")
        self.assertEqual(out["preferred_mode_ignored_reason"], "soft_gate_no_hard_block")

    def test_soft_gate_no_hard_block_ignores_conservative_preference_for_all_aia_statuses(self) -> None:
        for status in ["CAUTION_DIRECTIONAL", "AVOID_HARD", "AVOID", "WEAK", "OPEN"]:
            with self.subTest(status=status):
                out = sr.evaluate_aia_gate(
                    {"status": status, "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
                    cfg(),
                )
                self.assertTrue(out["allowed"])
                self.assertEqual(out["selected_mode"], "aggressive")
                self.assertEqual(out["selected_mode_source"], "scheduled_default_soft_no_hard_block")
                self.assertEqual(out["preferred_mode_ignored_reason"], "soft_gate_no_hard_block")

    def test_soft_avoid_without_hard_block_can_downgrade_when_enabled(self) -> None:
        c = cfg()
        c.preferred_mode_downgrade_enabled = True
        c.soft_avoid_downgrade = True
        out = sr.evaluate_aia_gate(
            {"status": "AVOID", "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
            c,
        )
        self.assertTrue(out["allowed"])
        self.assertTrue(out["aia_avoid_soft_allowed"])
        self.assertTrue(out["preferred_mode_downgrade_enabled"])
        self.assertTrue(out["soft_avoid_downgrade"])
        self.assertEqual(out["selected_mode"], "conservative")
        self.assertEqual(out["selected_mode_source"], "aia_preferred_mode_soft_downgrade")
        self.assertEqual(out["preferred_mode_ignored_reason"], "")

    def test_soft_preferred_mode_downgrade_env_enables_old_downgrade_path(self) -> None:
        with patch.dict("os.environ", {"SCHEDULED_SIGNAL_SOFT_PREFERRED_MODE_DOWNGRADE": "1"}, clear=False):
            c = sr.SchedulerConfig.from_env()
        out = sr.evaluate_aia_gate(
            {"status": "AVOID_HARD", "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
            c,
        )
        self.assertTrue(out["allowed"])
        self.assertTrue(out["preferred_mode_downgrade_enabled"])
        self.assertTrue(out["soft_avoid_downgrade"])
        self.assertEqual(out["selected_mode"], "conservative")
        self.assertEqual(out["selected_mode_source"], "aia_preferred_mode_soft_downgrade")

    def test_soft_avoid_with_hard_reasons_blocks(self) -> None:
        out = sr.evaluate_aia_gate(
            {
                "status": "AVOID",
                "preferred_mode": "conservative",
                "event_risk_level": "severe",
                "confirm_policy": "block_stale_confirm",
                "event_bias": "risk_off",
                "focus_asset": "SOL",
                "focus_direction": "LONG",
            },
            cfg(),
        )
        self.assertFalse(out["allowed"])
        self.assertFalse(out["aia_avoid_soft_allowed"])
        self.assertFalse(out["soft_avoid_downgrade"])
        self.assertIn("counter_risk_without_regime_confirmation", out["hard_block_reasons"])

    def test_aia_background_severe_btc_long_becomes_tactical_not_hard_block(self) -> None:
        out = sr.evaluate_aia_gate(
            {
                "status": "AVOID_HARD",
                "preferred_mode": "conservative",
                "event_risk_level": "severe",
                "confirm_policy": "block_stale_confirm",
                "event_bias": "risk_off",
                "focus_asset": "BTC",
                "focus_direction": "LONG",
                "flow_bias": "bullish",
            },
            cfg(),
        )
        self.assertTrue(out["allowed"])
        self.assertEqual(out["execution_mode"], "TACTICAL_CONFIRM_ONLY")
        self.assertEqual(out["entry_mode_required"], "WAIT_CONFIRM")

    def test_strict_mode_blocks_avoid(self) -> None:
        c = cfg()
        c.gate_mode = "strict"
        out = sr.evaluate_aia_gate({"status": "AVOID"}, c)
        self.assertFalse(out["allowed"])
        self.assertIn("aia_status_avoid", out["hard_block_reasons"])

    def test_off_mode_never_blocks_but_keeps_context(self) -> None:
        c = cfg()
        c.gate_mode = "off"
        out = sr.evaluate_aia_gate(
            {
                "status": "AVOID",
                "event_risk_level": "severe",
                "confirm_policy": "block_stale_confirm",
                "event_bias": "risk_off",
                "focus_asset": "SOL",
                "focus_direction": "LONG",
            },
            c,
        )
        self.assertTrue(out["allowed"])
        self.assertEqual(out["aia_status"], "AVOID")

    def test_severe_block_stale_confirm_blocks_only_on_conflict(self) -> None:
        base = {
            "event_risk_level": "severe",
            "confirm_policy": "block_stale_confirm",
            "event_bias": "risk_off",
        }
        blocked = sr.evaluate_aia_gate({**base, "focus_asset": "SOL", "focus_direction": "LONG"}, cfg())
        allowed = sr.evaluate_aia_gate({**base, "focus_asset": "BTC", "focus_direction": "SHORT"}, cfg())
        self.assertFalse(blocked["allowed"])
        self.assertTrue(allowed["allowed"])

    def test_day_execution_policy_conflict_is_audited_not_forced(self) -> None:
        out = sr.day_execution_policy_conflict(
            {
                "price_regime": "risk_on",
                "day_preferred_direction": "LONG",
                "day_focus": ["BTC", "ETH"],
                "execution_mode": "BLOCKED",
                "reason": "counter_risk_without_regime_confirmation",
            }
        )
        self.assertTrue(out["day_execution_policy_conflict"])
        self.assertEqual(out["downstream_execution_mode"], "BLOCKED")

    def test_severe_headline_escalation_blocks_counter_trend_without_regime_flip(self) -> None:
        blocked = sr.evaluate_aia_gate(
            {
                "event_risk_level": "severe",
                "headline_risk_delta": "ESCALATION",
                "event_bias": "risk_off",
                "focus_asset": "BTC",
                "focus_direction": "LONG",
            },
            cfg(),
        )
        allowed = sr.evaluate_aia_gate(
            {
                "event_risk_level": "severe",
                "headline_risk_delta": "ESCALATION",
                "event_bias": "risk_off",
                "focus_asset": "BTC",
                "focus_direction": "LONG",
                "explicit_regime_flip_reason": "spot_absorption_overrides_headline_shock",
            },
            cfg(),
        )

        self.assertFalse(blocked["allowed"])
        self.assertIn("counter_trend_signal_during_escalation_without_regime_flip", blocked["hard_block_reasons"])
        self.assertTrue(allowed["allowed"])

    def test_severe_headline_escalation_allows_risk_off_aligned_short(self) -> None:
        out = sr.evaluate_aia_gate(
            {
                "event_risk_level": "severe",
                "headline_risk_delta": "ESCALATION",
                "event_bias": "risk_off",
                "focus_asset": "BTC",
                "focus_direction": "SHORT",
            },
            cfg(),
        )

        self.assertTrue(out["allowed"])
        self.assertEqual(out["headline_risk_delta"], "ESCALATION")

    def test_day_bias_conflict_requires_explicit_regime_flip_reason(self) -> None:
        blocked = sr.evaluate_aia_gate(
            {
                "status": "OPEN",
                "day_bias": "long",
                "focus_asset": "BTC",
                "focus_direction": "short",
            },
            cfg(),
        )
        allowed = sr.evaluate_aia_gate(
            {
                "status": "OPEN",
                "day_bias": "long",
                "focus_asset": "BTC",
                "focus_direction": "short",
                "regime_flip_reason": "h1_ema_loss_and_risk_off_shift",
            },
            cfg(),
        )

        self.assertFalse(blocked["allowed"])
        self.assertIn("counter_day_bias_without_regime_flip", blocked["hard_block_reasons"])
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["reason"], "allowed")


class TestScheduledSignalState(unittest.TestCase):
    async def _inline_to_thread(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def _candidate(self, **overrides) -> dict:
        base = sr._candidate_from_payload(
            {
                "symbol": "BNB/USDT",
                "direction": "long",
                "entry_range": [610.0, 610.28],
                "sl": 604.04,
                "tp1": 616.4,
                "tp2": 628.6,
                "rr": 3.0,
                "confidence": "high",
                "entry_mode": "wait_confirm",
                "holding_horizon": "intraday_to_1_2d",
                "mode": "neutral",
                "ema20_m15": 610.14,
            },
            signal_id="20260614_123006",
        )
        base.update(overrides)
        return base

    def _old(self, status: str = "WAIT_CONFIRM", **overrides) -> dict:
        base = {
            "signal_id": "20260614_093003",
            "status": status,
            "symbol": "BNBUSDT",
            "display_symbol": "BNB/USDT",
            "direction": "long",
            "entry_price": 610.2,
            "sl": 604.1,
            "tp1": 623.2,
            "tp2": 629.3,
            "rr": 3.13,
            "confidence": "high",
            "confidence_rank": 3,
            "mode": "neutral",
            "holding_horizon": "intraday_to_1_2d",
            "strategy_type": "wait_confirm",
            "filled": status in sr.LIVE_POSITION_STATUSES,
        }
        base.update(overrides)
        return base

    def _agent_state(self, *, active: bool = True) -> dict:
        if not active:
            return {"active_scenarios": {}, "trades": {}}
        return {
            "active_scenarios": {
                "BNB/USDT": {
                    "long": {
                        "primary_signal_id": "20260614_093003",
                        "primary_lifecycle_state": "TIMEOUT_LIVE",
                        "primary_position_status": "OPEN",
                        "secondary_signal_ids": ["20260614_101500"],
                    }
                }
            },
            "trades": {
                "20260614_093003": {
                    "identity": {"signal_id": "20260614_093003"},
                    "lifecycle": {"lifecycle_state": "TIMEOUT_LIVE"},
                    "position": {"position_status": "OPEN"},
                    "signal_snapshot": {
                        "entry": 610.2,
                        "sl": 604.1,
                        "rr": 3.13,
                        "strategy_type": "wait_confirm",
                        "mode": "neutral",
                    },
                }
            },
        }

    def _write_agent_state(self, tmpdir: str, state: dict | None = None) -> Path:
        path = Path(tmpdir) / "agent_trade_state.json"
        path.write_text(json.dumps(state if state is not None else self._agent_state()), encoding="utf-8")
        return path

    def test_wait_confirm_near_duplicate_with_improved_tvh_replaces(self) -> None:
        candidate = self._candidate(entry_price=610.14, sl=604.1, rr=3.2, ema20_m15=610.14)
        decision = sr.evaluate_duplicate_publication(candidate, [self._old(rr=3.0)])
        self.assertEqual(decision["publication_type"], "replace_wait_confirm")
        self.assertTrue(decision["replacement_selected"])
        self.assertEqual(decision["replaced_signal_id"], "20260614_093003")

    def test_wait_confirm_near_duplicate_without_material_improvement_updates(self) -> None:
        candidate = self._candidate(entry_price=610.14, sl=604.04, rr=3.0, ema20_m15=610.0)
        decision = sr.evaluate_duplicate_publication(candidate, [self._old(rr=3.1)])
        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertFalse(decision["replacement_selected"])

    def test_wait_confirm_headline_escalation_requires_management_review(self) -> None:
        candidate = self._candidate(
            entry_price=610.14,
            sl=604.04,
            event_risk={
                "headline_risk_delta": "ESCALATION",
                "previous_risk_level": "severe",
                "new_risk_level": "severe",
                "risk_transition_reason": "military_action",
                "headline_update_generated": True,
            },
        )
        decision = sr.evaluate_duplicate_publication(candidate, [self._old("WAIT_CONFIRM", rr=3.1)])

        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertEqual(decision["headline_risk_delta"], "ESCALATION")
        self.assertTrue(decision["headline_update_generated"])
        self.assertTrue(decision["confirmation_required"])
        self.assertTrue(decision["management_review_required"])
        self.assertEqual(decision["management_review_reason"], "headline_risk_escalation")

    def test_wait_confirm_near_duplicate_with_worse_rr_updates(self) -> None:
        candidate = self._candidate(entry_price=610.14, sl=603.0, rr=2.0, ema20_m15=610.14)
        decision = sr.evaluate_duplicate_publication(candidate, [self._old(rr=3.0)])
        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertFalse(decision["replacement_selected"])

    def test_setup_armed_near_duplicate_does_not_replace(self) -> None:
        decision = sr.evaluate_duplicate_publication(self._candidate(), [self._old("SETUP_ARMED", filled=False)])
        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertFalse(decision["replacement_selected"])

    def test_confirm_live_material_reprice_still_blocks_independent_full_signal(self) -> None:
        candidate = self._candidate(entry_price=626.8, sl=620.03, rr=3.4)
        decision = sr.evaluate_duplicate_publication(candidate, [self._old("CONFIRM_LIVE", filled=False, position_status="NONE")])
        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertEqual(decision["duplicate_signal_status"], "CONFIRM_LIVE")
        self.assertEqual(decision["replacement_reason"], "existing_signal_confirmed_or_armed")

    def test_setup_armed_material_reprice_still_blocks_independent_full_signal(self) -> None:
        candidate = self._candidate(entry_price=626.8, sl=620.03, rr=3.4)
        decision = sr.evaluate_duplicate_publication(candidate, [self._old("SETUP_ARMED", filled=False, position_status="NONE")])
        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertEqual(decision["duplicate_signal_status"], "SETUP_ARMED")
        self.assertEqual(decision["replacement_reason"], "existing_signal_confirmed_or_armed")

    def test_execution_modes_are_compatible_for_same_direction_duplicate_detection(self) -> None:
        candidate = self._candidate(mode="aggressive", entry_price=610.14, sl=604.04)
        decision = sr.evaluate_duplicate_publication(candidate, [self._old(mode="neutral", rr=3.1)])
        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertTrue(decision["strategy_same_or_compatible"])

    def test_entry_live_or_hold_near_duplicate_updates_management(self) -> None:
        for status in ("ENTRY_LIVE", "HOLD"):
            decision = sr.evaluate_duplicate_publication(self._candidate(), [self._old(status)])
            self.assertEqual(decision["publication_type"], "active_signal_update")
            self.assertFalse(decision["replacement_selected"])

    def test_managed_status_near_duplicate_updates_management(self) -> None:
        for status in (
            "REDUCE",
            "PARTIALLY_REDUCED",
            "TP1_HIT_LIVE",
            "TP2_HIT_LIVE",
            "RUNNER_ACTIVE",
            "RUNNER_ACTIVE_TO_TP3",
            "EXTEND_RUNNER",
            "TRAIL_STOP_ACTIVE",
            "TRAIL_STOP",
        ):
            with self.subTest(status=status):
                decision = sr.evaluate_duplicate_publication(
                    self._candidate(),
                    [self._old(status, position_status="PARTIALLY_REDUCED")],
                    now_utc=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
                )
                self.assertEqual(decision["publication_type"], "active_signal_update")
                self.assertTrue(decision["duplicate_in_work_signal_detected"])

    def test_sol_near_duplicate_regression_updates_active_signal(self) -> None:
        candidate = sr._candidate_from_payload(
            {
                "symbol": "SOL/USDT",
                "direction": "long",
                "entry_price": 71.60,
                "sl": 70.88,
                "tp1": 72.32,
                "tp2": 73.03,
                "tp3": 73.75,
                "mode": "neutral",
            },
            signal_id="20260621_003000",
        )
        old = {
            **self._old(
                "ENTRY_LIVE",
                signal_id="20260620_153003",
                symbol="SOLUSDT",
                display_symbol="SOL/USDT",
                entry_price=71.67,
                sl=70.95,
                tp1=72.49,
                tp2=73.10,
                position_status="OPEN",
                ts="2026-06-20T14:53:00Z",
            ),
            "tp3": 73.82,
        }

        decision = sr.evaluate_duplicate_publication(
            candidate,
            [old],
            now_utc=datetime(2026, 6, 20, 21, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["publication_type"], "active_signal_update")
        self.assertTrue(decision["duplicate_in_work_signal_detected"])
        self.assertEqual(decision["duplicate_signal_id"], "20260620_153003")
        self.assertEqual(decision["replacement_reason"], "existing_signal_live_or_management")
        self.assertAlmostEqual(decision["entry_distance_pct"], 0.09767, places=4)
        self.assertAlmostEqual(decision["sl_distance_pct"], 0.09866, places=4)

    def test_wait_confirm_before_aia_360m_deadline_is_not_ignored(self) -> None:
        now = datetime(2026, 6, 27, 9, 30, tzinfo=timezone.utc)
        candidate = sr._candidate_from_payload(
            {
                "symbol": "SOL/USDT",
                "direction": "long",
                "entry_price": 71.62,
                "sl": 70.90,
                "tp1": 72.34,
                "tp2": 73.91,
                "mode": "aggressive",
                "entry_mode": "wait_confirm",
                "holding_horizon": "intraday_to_1_2d",
            },
            signal_id="20260627_123002",
        )
        old = self._old(
            "WAIT_CONFIRM",
            signal_id="20260627_093004",
            symbol="SOLUSDT",
            display_symbol="SOL/USDT",
            entry_price=71.60,
            sl=70.88,
            tp1=72.55,
            tp2=73.91,
            ts="2026-06-27T06:33:39Z",
            filled=False,
            position_status="",
            rr=3.2,
        )

        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)

        self.assertIn(decision["publication_type"], {"active_signal_update", "replace_wait_confirm"})
        self.assertTrue(decision["duplicate_in_work_signal_detected"])
        self.assertEqual(decision["duplicate_signal_id"], "20260627_093004")
        self.assertFalse(decision["stale_active_signal_ignored"])
        self.assertLess(decision["entry_distance_pct"], 0.05)
        self.assertLess(decision["sl_distance_pct"], 0.05)

    def test_wait_confirm_after_aia_360m_deadline_can_be_ignored(self) -> None:
        now = datetime(2026, 6, 27, 12, 34, tzinfo=timezone.utc)
        candidate = sr._candidate_from_payload(
            {
                "symbol": "SOL/USDT",
                "direction": "long",
                "entry_price": 71.62,
                "sl": 70.90,
                "tp1": 72.34,
                "tp2": 73.91,
                "mode": "aggressive",
                "entry_mode": "wait_confirm",
                "holding_horizon": "intraday_to_1_2d",
            },
            signal_id="20260627_153002",
        )
        old = self._old(
            "WAIT_CONFIRM",
            signal_id="20260627_093004",
            symbol="SOLUSDT",
            display_symbol="SOL/USDT",
            entry_price=71.60,
            sl=70.88,
            ts="2026-06-27T06:33:39Z",
            filled=False,
            position_status="",
        )

        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)

        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertTrue(decision["stale_active_signal_ignored"])
        self.assertEqual(decision["stale_active_signal_reason"], "pending_state_ttl_expired")

    def test_opposite_direction_existing_signal_conflict_update(self) -> None:
        candidate = self._candidate(direction="short")
        decision = sr.evaluate_duplicate_publication(candidate, [self._old("HOLD")])
        self.assertEqual(decision["publication_type"], "conflict_update")
        self.assertEqual(decision["replacement_reason"], "opposite_direction_in_work_signal")

    def test_old_confirm_live_without_position_does_not_conflict_update(self) -> None:
        now = datetime(2026, 6, 17, 9, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        old = self._old(
            "CONFIRM_LIVE",
            signal_id="20260613_104510",
            ts="2026-06-13T07:45:10Z",
            filled=False,
            position_status="",
        )
        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)
        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertTrue(decision["stale_active_signal_ignored"])
        self.assertEqual(decision["stale_active_signal_id"], "20260613_104510")
        self.assertEqual(decision["stale_active_signal_status"], "CONFIRM_LIVE")
        self.assertEqual(decision["stale_active_signal_reason"], "pending_state_ttl_expired")

    def test_old_tp1_hit_without_runner_expires_as_blocker(self) -> None:
        now = datetime(2026, 6, 19, 15, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        old = self._old(
            "TP1_HIT_LIVE",
            signal_id="20260618_093003",
            direction="long",
            ts="2026-06-18T06:30:03Z",
            position_status="",
            runner_active=False,
        )

        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)

        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertTrue(decision["stale_tp1_hit_signal_ignored"])
        self.assertEqual(decision["stale_tp1_hit_signal_id"], "20260618_093003")
        self.assertEqual(decision["stale_tp1_hit_signal_reason"], "tp1_hit_without_runner_expired")

    def test_tp1_hit_with_runner_remains_active_blocker(self) -> None:
        now = datetime(2026, 6, 19, 15, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        old = self._old(
            "TP1_HIT_LIVE",
            signal_id="20260618_093003",
            direction="long",
            ts="2026-06-18T06:30:03Z",
            position_status="",
            runner_active=True,
        )

        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)

        self.assertEqual(decision["publication_type"], "conflict_update")
        self.assertFalse(decision["stale_tp1_hit_signal_ignored"])

    def test_sl_hit_live_is_not_in_work_blocker(self) -> None:
        candidate = self._candidate(direction="short")
        old = self._old("SL_HIT_LIVE", direction="long", ts="2026-06-19T13:50:04Z")

        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=datetime(2026, 6, 19, 15, 30, tzinfo=timezone.utc))

        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertFalse(decision["duplicate_in_work_signal_detected"])

    def test_recent_confirm_live_still_conflict_updates(self) -> None:
        now = datetime(2026, 6, 17, 9, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        old = self._old("CONFIRM_LIVE", ts="2026-06-17T06:30:00Z", filled=False, position_status="")
        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)
        self.assertEqual(decision["publication_type"], "urgent_review")
        self.assertTrue(decision["entry_blocked"])
        self.assertTrue(decision["human_decision_required"])
        self.assertFalse(decision["stale_active_signal_ignored"])

    def test_pre_entry_opposite_bias_message_is_urgent_and_not_auto_reverse(self) -> None:
        candidate = self._candidate(direction="short", confidence="low")
        old = self._old("SETUP_ARMED", direction="long", filled=False, position_status="NONE")
        decision = sr.evaluate_duplicate_publication(candidate, [old])
        message = sr.render_duplicate_update_message(decision, candidate)

        self.assertEqual(decision["publication_type"], "urgent_review")
        self.assertFalse(decision["propose_reverse_signal"])
        self.assertIn("opposite bias before entry", message)
        self.assertIn("вход ещё НЕ исполнен", message)
        self.assertIn("Это не новый автоматический вход", message)

    def test_active_opposite_bias_without_aia_decision_renders_pending(self) -> None:
        candidate = self._candidate(symbol="SOL/USDT", direction="short")
        decision = {
            "publication_type": "conflict_update",
            "duplicate_signal_id": "20260628_183003",
            "duplicate_signal_status": "ENTRY_LIVE",
            "duplicate_signal": {
                "signal_id": "20260628_183003",
                "status": "ENTRY_LIVE",
                "display_symbol": "SOL/USDT",
                "direction": "long",
            },
        }

        message = sr.render_duplicate_update_message(decision, candidate)

        self.assertIn("🔄 RE_EVAL_ACTIVE_SIGNAL", message)
        self.assertIn("AIA management decision запрошен", message)
        self.assertIn("Ждём финальное решение", message)
        self.assertNotIn("Нужна AIA management decision", message)

    def test_active_opposite_bias_with_reduce_renders_resolved_aia_action(self) -> None:
        candidate = self._candidate(symbol="SOL/USDT", direction="short")
        decision = {
            "publication_type": "conflict_update",
            "duplicate_signal_id": "20260628_183003",
            "duplicate_signal_status": "ENTRY_LIVE",
            "final_management_action": "REDUCE",
            "action_label": "сократить риск частично",
            "action_reason": "headline/event risk высокий; asset flow смешанный.",
            "action_confidence": 0.66,
            "next_check": 15,
            "urgency": "medium",
            "duplicate_signal": {
                "signal_id": "20260628_183003",
                "status": "ENTRY_LIVE",
                "display_symbol": "SOL/USDT",
                "direction": "long",
            },
        }

        message = sr.render_duplicate_update_message(decision, candidate)

        self.assertIn("🔄 RE_EVAL_ACTIVE_SIGNAL → AIA: REDUCE", message)
        self.assertIn("Решение AIA:", message)
        self.assertIn("REDUCE — сократить риск частично", message)
        self.assertIn("headline/event risk высокий", message)

    def test_active_opposite_bias_with_close_now_renders_resolved_aia_action(self) -> None:
        candidate = self._candidate(symbol="SOL/USDT", direction="short")
        decision = {
            "publication_type": "conflict_update",
            "duplicate_signal_id": "20260628_183003",
            "duplicate_signal_status": "ENTRY_LIVE",
            "management_action": "CLOSE_NOW",
            "action_reason": "структура сломана / зона инвалидации достигнута.",
            "duplicate_signal": {
                "signal_id": "20260628_183003",
                "status": "ENTRY_LIVE",
                "display_symbol": "SOL/USDT",
                "direction": "long",
            },
        }

        message = sr.render_duplicate_update_message(decision, candidate)

        self.assertIn("🔄 RE_EVAL_ACTIVE_SIGNAL → AIA: CLOSE_NOW", message)
        self.assertIn("структура сломана", message)

    def test_duplicate_management_message_includes_severe_headline_risk_advisory(self) -> None:
        candidate = self._candidate(symbol="BTC/USDT", direction="long")
        decision = {
            "publication_type": "management_update",
            "duplicate_signal_id": "20260627_183004",
            "duplicate_signal_status": "TIMEOUT_LIVE",
            "event_risk_level": "severe",
            "event_bias": "risk_off",
            "dominant_critical_topic": "strategic_shipping_energy_chokepoint",
            "duplicate_signal": {
                "signal_id": "20260627_183004",
                "status": "TIMEOUT_LIVE",
                "display_symbol": "BTC/USDT",
                "direction": "long",
            },
        }

        message = sr.render_duplicate_update_message(decision, candidate)

        self.assertIn("MANAGEMENT_UPDATE", message)
        self.assertIn("Новый сигнал не публикуем", message)
        self.assertIn("HEADLINE RISK UPDATE", message)
        self.assertIn("event_risk_level: severe", message)
        self.assertIn("event_bias: risk_off", message)
        self.assertIn("strategic_shipping_energy_chokepoint", message)
        self.assertIn("fresh management re-check", message)

    def test_duplicate_management_message_omits_headline_advisory_for_low_risk(self) -> None:
        candidate = self._candidate(symbol="BTC/USDT", direction="long")
        decision = {
            "publication_type": "management_update",
            "duplicate_signal_id": "20260627_183004",
            "duplicate_signal_status": "TIMEOUT_LIVE",
            "event_risk_level": "low",
            "event_bias": "neutral",
            "duplicate_signal": {
                "signal_id": "20260627_183004",
                "status": "TIMEOUT_LIVE",
                "display_symbol": "BTC/USDT",
                "direction": "long",
            },
        }

        message = sr.render_duplicate_update_message(decision, candidate)

        self.assertIn("MANAGEMENT_UPDATE", message)
        self.assertNotIn("HEADLINE RISK UPDATE", message)

    def test_state_guard_duplicate_suppression_passes_gate_risk_to_management_render_only(self) -> None:
        import tg_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            html = Path(tmpdir) / "signal_20260628_003001.html"
            log = Path(tmpdir) / "signal_20260628_003001.log"
            payload = {"symbol": "BTC/USDT", "direction": "long", "entry_range": [100.0, 101.0], "sl": 99.0, "tp1": 102.0, "tp2": 103.0}
            messages = []

            async def fake_publish_plain(_cfg, message, *, dry_run=False):
                messages.append(message)
                return [123]

            c = cfg(Path(tmpdir))
            gate = {
                "event_risk_level": "severe",
                "event_bias": "risk_off",
                "dominant_critical_topic": "strategic_shipping_energy_chokepoint",
            }
            guard = {
                "state_guard_status": "ok",
                "state_guard_decision": "MANAGEMENT_UPDATE",
                "state_guard_recommended_publication_type": "MANAGEMENT_UPDATE",
                "state_guard_primary_signal_id": "20260627_183004",
                "state_guard_primary_lifecycle_state": "TIMEOUT_LIVE",
                "state_guard_reason": "same_trade_idea_already_live",
                "state_guard_can_publish_full_signal": False,
            }
            with patch("scheduled_runner.run_command", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "scheduled_runner.read_last_signal_payload", return_value=payload
            ), patch.object(tg_bot, "_resolve_signal_run_artifacts", return_value=(html, log)), patch.object(
                tg_bot, "html_file_to_tg_text", return_value=["signal text"]
            ), patch.object(
                tg_bot, "_infer_signal_id", return_value="20260628_003001"
            ), patch(
                "scheduled_runner.evaluate_state_guard_shadow", return_value=guard
            ), patch(
                "scheduled_runner.publish_plain_message", fake_publish_plain
            ):
                result = asyncio.run(sr.generate_and_publish_signal("aggressive", c, dry_run=False, gate=gate))

        self.assertEqual(result["publication_type"], "management_update")
        self.assertEqual(result["aia_forward_mode"], "skipped_state_guard_enforcement")
        self.assertFalse(result["aia_forward_attempted"])
        self.assertIn("HEADLINE RISK UPDATE", messages[0])
        self.assertIn("strategic_shipping_energy_chokepoint", messages[0])

    def test_strong_pre_entry_opposite_bias_proposes_but_does_not_publish_reverse(self) -> None:
        candidate = self._candidate(direction="short", confidence="high")
        old = self._old("CONFIRM_LIVE", direction="long", filled=False, position_status="NONE")
        decision = sr.evaluate_duplicate_publication(candidate, [old])

        self.assertEqual(decision["publication_type"], "urgent_review")
        self.assertTrue(decision["propose_reverse_signal"])
        self.assertEqual(decision["old_setup_recommendation"], "CANCEL_ARMED_SETUP")
        self.assertNotEqual(decision["publication_type"], "full_signal")

    def test_old_entry_live_hold_open_position_still_conflict_updates(self) -> None:
        now = datetime(2026, 6, 17, 9, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        for status in ("ENTRY_LIVE", "HOLD"):
            with self.subTest(status=status):
                old = self._old(status, ts="2026-06-13T07:45:10Z", position_status="OPEN")
                decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)
                self.assertEqual(decision["publication_type"], "conflict_update")
                self.assertFalse(decision.get("entry_blocked", False))
                self.assertFalse(decision["stale_active_signal_ignored"])

    def test_old_wait_confirm_older_than_fallback_ttl_is_ignored(self) -> None:
        now = datetime(2026, 6, 17, 9, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        old = self._old("WAIT_CONFIRM", ts="2026-06-17T03:00:00Z", filled=False, position_status="")
        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)
        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertTrue(decision["stale_active_signal_ignored"])
        self.assertEqual(decision["stale_active_signal_reason"], "pending_state_ttl_expired")

    def test_advisory_states_do_not_block_full_signal(self) -> None:
        now = datetime(2026, 6, 17, 9, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        for status in ("MARKET_REPRICE_ALERT", "RE_EVAL_ACTIVE_SIGNAL", "ACTIVE_SIGNAL_UPDATE"):
            with self.subTest(status=status):
                decision = sr.evaluate_duplicate_publication(
                    candidate,
                    [self._old(status, ts="2026-06-17T09:00:00Z", filled=False, position_status="")],
                    now_utc=now,
                )
                self.assertEqual(decision["publication_type"], "full_signal")
                self.assertTrue(decision["stale_active_signal_ignored"])
                self.assertEqual(decision["stale_active_signal_reason"], "advisory_state_non_blocking")

    def test_missing_timestamp_old_signal_id_date_is_ignored(self) -> None:
        now = datetime(2026, 6, 17, 9, 30, tzinfo=timezone.utc)
        candidate = self._candidate(direction="short")
        old = self._old("CONFIRM_LIVE", signal_id="20260613_104510", filled=False, position_status="")
        decision = sr.evaluate_duplicate_publication(candidate, [old], now_utc=now)
        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertTrue(decision["stale_active_signal_ignored"])
        self.assertEqual(decision["stale_active_signal_reason"], "signal_id_date_expired")

    def test_terminal_existing_signal_allows_full_signal(self) -> None:
        candidate = self._candidate()
        old = self._old("EXPIRED_NO_CONFIRM")
        decision = sr.evaluate_duplicate_publication(candidate, [])
        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertFalse(decision["duplicate_in_work_signal_detected"])
        self.assertNotIn(old["status"], sr.IN_WORK_SIGNAL_STATUSES)

    def test_no_in_work_signal_allows_full_signal(self) -> None:
        decision = sr.evaluate_duplicate_publication(self._candidate(), [])
        self.assertEqual(decision["publication_type"], "full_signal")
        self.assertFalse(decision["duplicate_in_work_signal_detected"])

    def test_state_guard_shadow_management_update_logged_without_publication_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            c.state_guard_state_path = self._write_agent_state(tmpdir)
            candidate = self._candidate(entry_price=610.14, sl=604.04, rr=3.0)
            guard = sr.evaluate_state_guard_shadow(candidate, c, now_utc=datetime.now(timezone.utc))
            self.assertEqual(guard["state_guard_status"], "ok")
            self.assertEqual(guard["state_guard_decision"], "MANAGEMENT_UPDATE")
            self.assertFalse(guard["state_guard_can_publish_full_signal"])
            self.assertTrue(guard["active_same_direction_scenario_found"])
            self.assertEqual(guard["active_same_direction_signal_id"], "20260614_093003")
            self.assertEqual(guard["active_same_direction_status"], "TIMEOUT_LIVE")
            self.assertTrue(guard["duplicate_detected"])

            state = {"slots": {}}
            slot_time = sr.slot_datetime_msk(date(2026, 6, 5), "09:30")
            result = {
                "published": True,
                "reason": "published",
                "signal_id": "20260614_123006",
                "candidate_signal_id": "20260614_123006",
                "publication_type": "full_signal",
                "last_payload": {
                    "symbol": "BNB/USDT",
                    "direction": "long",
                    "entry_range": [610.0, 610.28],
                    "sl": 604.04,
                    "tp1": 616.4,
                    "tp2": 628.6,
                    "rr": 3.0,
                },
                **guard,
            }
            now = datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc)
            with patch("scheduled_runner.LOGS_DIR", Path(tmpdir) / "logs"), patch(
                "scheduled_runner.load_aia_context", return_value={"status": "OPEN"}
            ), patch("scheduled_runner.generate_and_publish_signal", return_value=result):
                asyncio.run(sr.run_signal_slot(now, c, state, "20260605_0930", slot_time, 1, dry_run=False))

            row = json.loads((Path(tmpdir) / "logs" / "scheduled_signal_decisions_20260605.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(row["decision"], "publish")
            self.assertEqual(row["publication_type"], "full_signal")
            self.assertEqual(row["state_guard_decision"], "MANAGEMENT_UPDATE")
            self.assertFalse(row["state_guard_can_publish_full_signal"])
            self.assertEqual(state["slots"]["20260605_0930"]["status"], "published")

    def test_state_guard_shadow_missing_state_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            c.state_guard_state_path = Path(tmpdir) / "missing_agent_trade_state.json"
            guard = sr.evaluate_state_guard_shadow(self._candidate(), c, now_utc=datetime.now(timezone.utc))
            self.assertEqual(guard["state_guard_status"], "unavailable")

    def test_state_guard_shadow_stale_state_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            c.state_guard_state_path = self._write_agent_state(tmpdir)
            c.state_guard_max_age_minutes = 15
            old = datetime(2026, 6, 5, 6, 0, tzinfo=timezone.utc).timestamp()
            os.utime(c.state_guard_state_path, (old, old))
            guard = sr.evaluate_state_guard_shadow(
                self._candidate(),
                c,
                now_utc=datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(guard["state_guard_status"], "stale")

    def test_state_guard_shadow_guard_exception_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            c.state_guard_state_path = self._write_agent_state(tmpdir)

            def fake_loader():
                def raise_guard(candidate, state):
                    raise RuntimeError("guard failed")

                return raise_guard

            with patch("scheduled_runner._load_state_guard_api", side_effect=fake_loader), self.assertLogs("scheduled_runner", level="ERROR"):
                guard = sr.evaluate_state_guard_shadow(self._candidate(), c, now_utc=datetime.now(timezone.utc))
            self.assertEqual(guard["state_guard_status"], "error")
            self.assertIn("guard failed", guard["state_guard_explanation"])

    def test_state_guard_shadow_allow_full_signal_when_no_active_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            c.state_guard_state_path = self._write_agent_state(tmpdir, self._agent_state(active=False))
            guard = sr.evaluate_state_guard_shadow(self._candidate(), c, now_utc=datetime.now(timezone.utc))
            self.assertEqual(guard["state_guard_status"], "ok")
            self.assertEqual(guard["state_guard_decision"], "ALLOW_FULL_SIGNAL")
            self.assertTrue(guard["state_guard_can_publish_full_signal"])

    def test_state_guard_enforcement_uses_active_direction_for_management_updates(self) -> None:
        c = cfg()
        guard = {
            "state_guard_status": "ok",
            "state_guard_decision": "MANAGEMENT_UPDATE",
            "state_guard_recommended_publication_type": "MANAGEMENT_UPDATE",
            "state_guard_primary_signal_id": "20260707_123004",
            "state_guard_primary_lifecycle_state": "ENTRY_LIVE",
            "state_guard_primary_position_status": "OPEN",
            "state_guard_primary_direction": "short",
            "state_guard_primary_entry": 610.0,
            "state_guard_primary_sl": 620.0,
            "state_guard_can_publish_full_signal": False,
            "state_guard_reason": "same_trade_idea_already_live",
        }
        candidate = self._candidate(
            symbol="BTC/USDT",
            display_symbol="BTC/USDT",
            direction="long",
            signal_id="20260708_103000",
        )

        enforcement = sr.build_state_guard_enforcement_decision(guard, candidate, c)
        message = sr.render_duplicate_update_message(enforcement, candidate)

        self.assertEqual(enforcement["duplicate_signal"]["direction"], "short")
        self.assertIn("BTC/USDT SHORT уже в работе", message)
        self.assertNotIn("BTC/USDT LONG уже в работе", message)

    def test_state_guard_duplicate_runtime_enforcement_disabled_logs_shadow_only(self) -> None:
        c = cfg()
        guard = {
            "state_guard_decision": "ACTIVE_SIGNAL_UPDATE",
            "state_guard_recommended_publication_type": "ACTIVE_SIGNAL_UPDATE",
            "state_guard_duplicate_detected": True,
            "state_guard_active_same_direction_scenario_found": True,
            "state_guard_primary_signal_id": "20260614_093003",
            "state_guard_can_publish_full_signal": False,
            "state_guard_primary_lifecycle_state": "SETUP_ARMED",
            "state_guard_entry_distance_pct": 0.062956,
            "state_guard_sl_distance_pct": 0.124711,
        }

        action = sr.state_guard_duplicate_runtime_action(guard, c)

        self.assertFalse(action["scheduled_state_guard_duplicate_enforcement_enabled"])
        self.assertTrue(action["scheduled_state_guard_duplicate_runtime_eligible"])
        self.assertEqual(action["scheduled_state_guard_duplicate_runtime_action"], "shadow_only")

    def test_state_guard_duplicate_runtime_enforcement_enabled_suppresses_full_signal(self) -> None:
        c = cfg()
        c.scheduled_state_guard_duplicate_enforcement_enabled = True
        guard = {
            "state_guard_decision": "MANAGEMENT_UPDATE",
            "state_guard_recommended_publication_type": "MANAGEMENT_UPDATE",
            "state_guard_duplicate_detected": True,
            "state_guard_active_same_direction_scenario_found": True,
            "state_guard_primary_signal_id": "20260614_093003",
            "state_guard_can_publish_full_signal": False,
            "state_guard_primary_lifecycle_state": "TP2_HIT_LIVE",
            "state_guard_entry_distance_pct": 0.062956,
            "state_guard_sl_distance_pct": 0.124711,
        }

        action = sr.state_guard_duplicate_runtime_action(guard, c)

        self.assertTrue(action["scheduled_state_guard_duplicate_enforcement_enabled"])
        self.assertTrue(action["scheduled_state_guard_duplicate_runtime_eligible"])
        self.assertEqual(action["scheduled_state_guard_duplicate_runtime_action"], "enforce_duplicate_suppression")
        self.assertEqual(action["duplicate_enforcement_action"], "enforce_duplicate_suppression")

    def test_state_guard_duplicate_runtime_suppresses_armed_setup_even_without_near_duplicate_flag(self) -> None:
        c = cfg()
        c.scheduled_state_guard_duplicate_enforcement_enabled = True
        guard = {
            "state_guard_decision": "ACTIVE_SIGNAL_UPDATE",
            "state_guard_recommended_publication_type": "ACTIVE_SIGNAL_UPDATE",
            "state_guard_duplicate_detected": False,
            "state_guard_active_same_direction_scenario_found": True,
            "state_guard_can_publish_full_signal": False,
            "state_guard_primary_signal_id": "20260701_163003",
            "state_guard_primary_lifecycle_state": "CONFIRM_LIVE",
            "state_guard_entry_distance_pct": 2.591504,
            "state_guard_sl_distance_pct": 2.521575,
        }

        action = sr.state_guard_duplicate_runtime_action(guard, c)

        self.assertTrue(action["scheduled_state_guard_duplicate_runtime_eligible"])
        self.assertEqual(action["scheduled_state_guard_duplicate_runtime_action"], "enforce_duplicate_suppression")

    def test_tp_ladder_validation_flags_duplicate_targets(self) -> None:
        validation = sr._tp_ladder_validation("long", 76.93, 78.88, 78.88, None)

        self.assertTrue(validation["invalid_tp_ladder"])
        self.assertTrue(validation["duplicate_tp_targets"])
        self.assertIn("tp1_equals_tp2", validation["tp_ladder_warnings"])

    def test_state_guard_duplicate_runtime_does_not_suppress_opposite_direction_conflict(self) -> None:
        c = cfg()
        c.scheduled_state_guard_duplicate_enforcement_enabled = True
        guard = {
            "state_guard_decision": "CONFLICT_UPDATE",
            "state_guard_recommended_publication_type": "CONFLICT_UPDATE",
            "state_guard_duplicate_detected": False,
            "state_guard_conflict_detected": True,
            "state_guard_can_publish_full_signal": False,
            "state_guard_primary_lifecycle_state": "ENTRY_LIVE",
            "state_guard_entry_distance_pct": 0.1,
            "state_guard_sl_distance_pct": 0.1,
        }

        action = sr.state_guard_duplicate_runtime_action(guard, c)

        self.assertTrue(action["scheduled_state_guard_duplicate_enforcement_enabled"])
        self.assertFalse(action["scheduled_state_guard_duplicate_runtime_eligible"])
        self.assertEqual(action["scheduled_state_guard_duplicate_runtime_action"], "none")

    def test_state_guard_enforcement_blocks_full_signal_and_publishes_management_update(self) -> None:
        import tg_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            html = Path(tmpdir) / "signal_20260606_153001.html"
            log = Path(tmpdir) / "signal_20260606_153001.log"
            payload = {"symbol": "SOL/USDT", "direction": "long", "entry_range": [100.0, 101.0], "sl": 99.0, "tp1": 102.0, "tp2": 103.0}
            messages = []

            async def fake_publish_plain(_cfg, message, *, dry_run=False):
                messages.append(message)
                return [123]

            async def should_not_publish(*args, **kwargs):
                raise AssertionError("full_signal publish path must not be used")

            c = cfg(Path(tmpdir))
            guard = {
                "state_guard_status": "ok",
                "state_guard_decision": "MANAGEMENT_UPDATE",
                "state_guard_recommended_publication_type": "MANAGEMENT_UPDATE",
                "state_guard_primary_signal_id": "20260701_183003",
                "state_guard_primary_lifecycle_state": "TP2_HIT_LIVE",
                "state_guard_reason": "same_trade_idea_already_live",
                "state_guard_can_publish_full_signal": False,
                "state_guard_primary_entry": 76.8,
                "state_guard_primary_sl": 76.03,
            }
            with patch("scheduled_runner.run_command", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "scheduled_runner.read_last_signal_payload", return_value=payload
            ), patch.object(tg_bot, "_resolve_signal_run_artifacts", return_value=(html, log)), patch.object(
                tg_bot, "html_file_to_tg_text", return_value=["signal text"]
            ), patch.object(
                tg_bot, "_infer_signal_id", return_value="20260606_153001"
            ), patch(
                "scheduled_runner.evaluate_state_guard_shadow", return_value=guard
            ), patch(
                "scheduled_runner.publish_plain_message", fake_publish_plain
            ), patch.object(
                tg_bot, "_publish_signal_result", should_not_publish
            ):
                result = asyncio.run(sr.generate_and_publish_signal("aggressive", c, dry_run=False))

            self.assertTrue(result["published"])
            self.assertEqual(result["publication_type"], "management_update")
            self.assertEqual(result["scheduled_state_guard_enforcement_action"], "enforce_non_full_signal")
            self.assertFalse(result["state_guard_manual_override_used"])
            self.assertIn("Новый сигнал не публикуем", messages[0])

    def test_state_guard_enforcement_suppress_duplicate_never_emits_full_signal(self) -> None:
        import tg_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            html = Path(tmpdir) / "signal_20260606_153001.html"
            log = Path(tmpdir) / "signal_20260606_153001.log"
            payload = {"symbol": "SOL/USDT", "direction": "long", "entry_range": [100.0, 101.0], "sl": 99.0, "tp1": 102.0, "tp2": 103.0}
            messages = []

            async def fake_publish_plain(_cfg, message, *, dry_run=False):
                messages.append(message)
                return [123]

            async def should_not_publish(*args, **kwargs):
                raise AssertionError("full_signal publish path must not be used")

            c = cfg(Path(tmpdir))
            guard = {
                "state_guard_status": "ok",
                "state_guard_decision": "SUPPRESS_DUPLICATE",
                "state_guard_recommended_publication_type": "SUPPRESS_DUPLICATE",
                "state_guard_primary_signal_id": "20260701_183003",
                "state_guard_primary_lifecycle_state": "TERMINAL",
                "state_guard_reason": "reentry_requires_fresh_reset",
                "state_guard_can_publish_full_signal": False,
            }
            with patch("scheduled_runner.run_command", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "scheduled_runner.read_last_signal_payload", return_value=payload
            ), patch.object(tg_bot, "_resolve_signal_run_artifacts", return_value=(html, log)), patch.object(
                tg_bot, "html_file_to_tg_text", return_value=["signal text"]
            ), patch.object(
                tg_bot, "_infer_signal_id", return_value="20260606_153001"
            ), patch(
                "scheduled_runner.evaluate_state_guard_shadow", return_value=guard
            ), patch(
                "scheduled_runner.publish_plain_message", fake_publish_plain
            ), patch.object(
                tg_bot, "_publish_signal_result", should_not_publish
            ):
                result = asyncio.run(sr.generate_and_publish_signal("aggressive", c, dry_run=False))

            self.assertEqual(result["publication_type"], "suppress_duplicate")
            self.assertEqual(result["scheduled_state_guard_enforcement_action"], "enforce_non_full_signal")
            self.assertIn("RE-ENTRY BLOCKED", messages[0])
            self.assertIn("Новый full_signal не публикуем", messages[0])

    def test_terminal_suppress_duplicate_uses_reentry_blocked_wording(self) -> None:
        message = sr.render_duplicate_update_message(
            {
                "publication_type": "suppress_duplicate",
                "duplicate_signal_id": "20260703_013003",
                "duplicate_signal_status": "TERMINAL",
                "state_guard_reason": "reentry_requires_fresh_reset",
                "state_guard_reentry_reason": "missing_fresh_pullback_or_reset",
                "duplicate_signal": {
                    "signal_id": "20260703_013003",
                    "status": "TERMINAL",
                    "display_symbol": "ETH/USDT",
                    "direction": "long",
                },
            },
            {"display_symbol": "ETH/USDT", "direction": "long"},
        )

        self.assertIn("TERMINAL / RE-ENTRY BLOCKED", message)
        self.assertIn("Последний связанный сигнал", message)
        self.assertNotIn("активный/связанный сценарий", message)
        self.assertNotIn("Активный сигнал", message)
        self.assertIn("fresh pullback/reset", message)

    def test_terminal_suppress_duplicate_includes_reentry_ttl_progress(self) -> None:
        message = sr.render_duplicate_update_message(
            {
                "publication_type": "suppress_duplicate",
                "duplicate_signal_id": "20260704_104718",
                "duplicate_signal_status": "TERMINAL",
                "state_guard_reentry_reason": "missing_fresh_pullback_or_reset",
                "state_guard_reentry_terminal_elapsed_hours": 12,
                "state_guard_reentry_terminal_ttl_hours": 24,
                "duplicate_signal": {
                    "signal_id": "20260704_104718",
                    "status": "TERMINAL",
                    "display_symbol": "ETH/USDT",
                    "direction": "long",
                },
            },
            {"display_symbol": "ETH/USDT", "direction": "long"},
        )

        self.assertIn("Re-entry заблокирован", message)
        self.assertIn("12 из 24 часов", message)

    def test_state_guard_enforcement_reentry_not_allowed_prevents_reentry_signal(self) -> None:
        enforcement = sr.build_state_guard_enforcement_decision(
            {
                "state_guard_status": "ok",
                "state_guard_can_publish_full_signal": False,
                "state_guard_decision": "RE_ENTRY_SIGNAL",
                "state_guard_recommended_publication_type": "RE_ENTRY_SIGNAL",
                "state_guard_reentry_allowed": False,
                "state_guard_primary_signal_id": "20260701_183003",
                "state_guard_primary_lifecycle_state": "TERMINAL",
                "state_guard_reason": "reentry_requires_fresh_reset",
            },
            self._candidate(signal_id="20260702_213003"),
            cfg(),
        )

        self.assertTrue(enforcement["state_guard_runtime_blocked_full_signal"])
        self.assertEqual(enforcement["publication_type"], "suppress_duplicate")

    def test_runner_management_rendering_used_for_active_runner(self) -> None:
        message = sr.render_duplicate_update_message(
            {
                "publication_type": "runner_management_update",
                "duplicate_signal_id": "20260701_183003",
                "duplicate_signal_status": "RUNNER_ACTIVE",
                "duplicate_signal": {
                    "signal_id": "20260701_183003",
                    "status": "RUNNER_ACTIVE",
                    "display_symbol": "SOL/USDT",
                    "direction": "long",
                },
            },
            {"display_symbol": "SOL/USDT", "direction": "long"},
        )

        self.assertIn("RUNNER MANAGEMENT UPDATE", message)
        self.assertIn("existing runner / add-on review only", message)

    def test_active_setup_rendering_used_for_setup_armed(self) -> None:
        message = sr.render_duplicate_update_message(
            {
                "publication_type": "active_signal_update",
                "duplicate_signal_id": "20260701_163003",
                "duplicate_signal_status": "SETUP_ARMED",
                "duplicate_signal": {
                    "signal_id": "20260701_163003",
                    "status": "SETUP_ARMED",
                    "display_symbol": "SOL/USDT",
                    "direction": "long",
                    "entry_price": 76.04,
                },
                "pending_update_classification": "refresh_pending_setup",
                "previous_entry": 76.04,
                "new_entry": 76.8,
            },
            {"display_symbol": "SOL/USDT", "direction": "long", "entry_price": 76.8},
        )

        self.assertIn("ACTIVE SETUP UPDATE", message)
        self.assertIn("Это не новый независимый вход", message)

    def test_manual_override_allows_full_signal_only_with_explicit_flag_and_reason(self) -> None:
        import tg_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            html = Path(tmpdir) / "signal_20260606_153001.html"
            log = Path(tmpdir) / "signal_20260606_153001.log"
            payload = {"symbol": "SOL/USDT", "direction": "long", "entry_range": [100.0, 101.0], "sl": 99.0, "tp1": 102.0, "tp2": 103.0}
            publish_calls = []

            async def fake_publish(*args, **kwargs):
                publish_calls.append(kwargs)
                return True

            c = cfg(Path(tmpdir))
            c.scheduled_state_guard_manual_override_enabled = True
            c.scheduled_state_guard_manual_override_reason = "manual forensic override"
            guard = {
                "state_guard_status": "ok",
                "state_guard_decision": "SUPPRESS_DUPLICATE",
                "state_guard_recommended_publication_type": "SUPPRESS_DUPLICATE",
                "state_guard_primary_signal_id": "20260701_183003",
                "state_guard_primary_lifecycle_state": "SETUP_ARMED",
                "state_guard_reason": "manual_release",
                "state_guard_can_publish_full_signal": False,
            }
            with patch("scheduled_runner.run_command", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "scheduled_runner.read_last_signal_payload", return_value=payload
            ), patch(
                "scheduled_runner.load_in_work_signal_state", return_value=[]
            ), patch(
                "scheduled_runner.make_context", return_value=SimpleNamespace(bot=SimpleNamespace(sent=[]))
            ), patch(
                "scheduled_runner.asyncio.to_thread", self._inline_to_thread
            ), patch.object(tg_bot, "_resolve_signal_run_artifacts", return_value=(html, log)), patch.object(
                tg_bot, "html_file_to_tg_text", return_value=["signal text"]
            ), patch.object(
                tg_bot, "_infer_signal_id", return_value="20260606_153001"
            ), patch.object(
                tg_bot, "_publish_signal_result", fake_publish
            ), patch.object(
                tg_bot, "_build_signal_json_v1", return_value={"signal_id": "20260606_153001"}
            ), patch.object(
                tg_bot, "send_signal_to_aia", return_value=True
            ), patch(
                "scheduled_runner.evaluate_state_guard_shadow", return_value=guard
            ):
                result = asyncio.run(sr.generate_and_publish_signal("aggressive", c, dry_run=False))

            self.assertTrue(result["published"])
            self.assertEqual(result["publication_type"], "full_signal")
            self.assertTrue(result["state_guard_manual_override_used"])
            self.assertEqual(result["scheduled_state_guard_enforcement_action"], "manual_override_allow_full_signal")
            self.assertEqual(result["state_guard_manual_override_reason"], "manual forensic override")
            self.assertEqual(len(publish_calls), 1)

    def test_state_guard_shadow_logs_reentry_diagnostics(self) -> None:
        result = {
            "signal_id": "20260614_123006",
            "last_payload": {
                "symbol": "BNB/USDT",
                "direction": "long",
                "entry_range": [610.0, 610.28],
                "sl": 604.04,
                "tp1": 616.4,
                "tp2": 628.6,
                "rr": 3.0,
            },
            "state_guard_status": "ok",
            "state_guard_decision": "RE_ENTRY_SIGNAL",
            "state_guard_can_publish_full_signal": True,
            "state_guard_recommended_publication_type": "RE_ENTRY_SIGNAL",
            "state_guard_reason": "previous_scenario_finalized_fresh_reentry",
            "state_guard_primary_signal_id": "20260614_093003",
            "state_guard_primary_lifecycle_state": "FINALIZE",
            "state_guard_primary_position_status": "CLOSED",
            "state_guard_previous_signal_id": "20260614_093003",
            "state_guard_previous_outcome": "close_after_tp2",
            "state_guard_previous_tp_reached": "TP2",
            "state_guard_previous_runner_status": "closed",
            "state_guard_reentry_signal": True,
            "state_guard_reentry_allowed": True,
            "state_guard_reentry_reason": "previous_tp_reached_and_fresh_pullback_reset",
        }

        row = sr.state_guard_shadow_log_row(datetime(2026, 6, 14, tzinfo=timezone.utc), "slot", result)

        self.assertEqual(row["guard_decision"], "RE_ENTRY_SIGNAL")
        self.assertEqual(row["previous_signal_id"], "20260614_093003")
        self.assertEqual(row["previous_tp_reached"], "TP2")
        self.assertTrue(row["reentry_signal"])
        self.assertTrue(row["reentry_allowed"])

    def test_day_bias_diagnostics_flags_risk_off_long_as_counter_regime(self) -> None:
        payload = {"day_mid_context": {"day_bias": "risk-off"}}
        candidate = {"direction": "long"}

        diag = sr.day_bias_diagnostics(payload, candidate, gate={})

        self.assertEqual(diag["day_bias_direction"], "short")
        self.assertEqual(diag["candidate_direction"], "long")
        self.assertEqual(diag["signal_direction_vs_day_bias"], "counter")
        self.assertTrue(diag["counter_regime_signal"])
        self.assertEqual(diag["counter_regime_allowed_reason"], "none")
        self.assertFalse(diag["market_override_detected"])

    def test_publish_signal_result_keeps_aia_forward_enabled_by_default(self) -> None:
        import tg_bot

        param = inspect.signature(tg_bot._publish_signal_result).parameters["skip_aia_forward"]
        self.assertFalse(param.default)

    def test_generate_and_publish_signal_awaits_aia_forward_success(self) -> None:
        import tg_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            html = Path(tmpdir) / "signal_20260606_153001.html"
            log = Path(tmpdir) / "signal_20260606_153001.log"
            payload = {"symbol": "BTC/USDT", "direction": "long", "entry_range": [100.0, 101.0], "sl": 99.0, "tp1": 102.0, "tp2": 103.0}
            publish_kwargs = []
            built_kwargs = []
            sent_payloads = []
            plain_messages = []

            async def fake_publish(*args, **kwargs):
                publish_kwargs.append(kwargs)
                return True

            async def fake_publish_plain(_cfg, message, *, dry_run=False):
                plain_messages.append(message)
                return [123]

            c = cfg(Path(tmpdir))
            guard = {
                "state_guard_status": "ok",
                "state_guard_decision": "ALLOW_FULL_SIGNAL",
                "state_guard_can_publish_full_signal": True,
                "state_guard_recommended_publication_type": "FULL_SIGNAL",
            }
            with patch("scheduled_runner.run_command", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "scheduled_runner.read_last_signal_payload",
                return_value=payload,
            ), patch(
                "scheduled_runner.make_context", return_value=SimpleNamespace(bot=SimpleNamespace(sent=[]))
            ), patch(
                "scheduled_runner.asyncio.to_thread", self._inline_to_thread
            ), patch.object(tg_bot, "_resolve_signal_run_artifacts", return_value=(html, log)), patch.object(
                tg_bot, "html_file_to_tg_text", return_value=["signal text"]
            ), patch.object(
                tg_bot, "_publish_signal_result", fake_publish
            ), patch.object(
                tg_bot, "_infer_signal_id", return_value="20260606_153001"
            ), patch.object(
                tg_bot, "_build_signal_json_v1", side_effect=lambda **kwargs: built_kwargs.append(kwargs) or {"signal_id": kwargs["signal_id"]}
            ), patch.object(
                tg_bot, "send_signal_to_aia", side_effect=lambda body: sent_payloads.append(body) or True
            ), patch(
                "scheduled_runner.load_in_work_signal_state", return_value=[]
            ), patch(
                "scheduled_runner.evaluate_state_guard_shadow", return_value=guard
            ), patch(
                "scheduled_runner.publish_plain_message", fake_publish_plain
            ):
                result = asyncio.run(sr.generate_and_publish_signal("aggressive", c, dry_run=False))

            self.assertTrue(result["published"])
            self.assertTrue(result["aia_forward_attempted"])
            self.assertTrue(result["aia_forward_ok"])
            self.assertIsNone(result["aia_forward_error"])
            self.assertEqual(result["aia_forward_mode"], "awaited_scheduled")
            self.assertEqual(sent_payloads, [{"signal_id": "20260606_153001"}])
            self.assertTrue(publish_kwargs[0]["skip_aia_forward"])
            self.assertEqual(built_kwargs[0]["publish_targets"], c.target_chat_ids)

    def test_generate_and_publish_signal_aia_failure_keeps_publish(self) -> None:
        import tg_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            html = Path(tmpdir) / "signal_20260606_153001.html"
            log = Path(tmpdir) / "signal_20260606_153001.log"
            payload = {"symbol": "BTC/USDT", "direction": "long", "entry_range": [100.0, 101.0], "sl": 99.0, "tp1": 102.0, "tp2": 103.0}
            plain_messages = []

            async def fake_publish(*args, **kwargs):
                return True

            async def fake_publish_plain(_cfg, message, *, dry_run=False):
                plain_messages.append(message)
                return [123]

            c = cfg(Path(tmpdir))
            guard = {
                "state_guard_status": "ok",
                "state_guard_decision": "ALLOW_FULL_SIGNAL",
                "state_guard_can_publish_full_signal": True,
                "state_guard_recommended_publication_type": "FULL_SIGNAL",
            }
            with patch("scheduled_runner.run_command", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "scheduled_runner.read_last_signal_payload",
                return_value=payload,
            ), patch(
                "scheduled_runner.make_context", return_value=SimpleNamespace(bot=SimpleNamespace(sent=[]))
            ), patch(
                "scheduled_runner.asyncio.to_thread", self._inline_to_thread
            ), patch.object(tg_bot, "_resolve_signal_run_artifacts", return_value=(html, log)), patch.object(
                tg_bot, "html_file_to_tg_text", return_value=["signal text"]
            ), patch.object(
                tg_bot, "_publish_signal_result", fake_publish
            ), patch.object(
                tg_bot, "_infer_signal_id", return_value="20260606_153001"
            ), patch.object(
                tg_bot, "_build_signal_json_v1", return_value={"signal_id": "20260606_153001"}
            ), patch.object(
                tg_bot, "send_signal_to_aia", side_effect=RuntimeError("aia down")
            ), patch(
                "scheduled_runner.load_in_work_signal_state", return_value=[]
            ), patch(
                "scheduled_runner.evaluate_state_guard_shadow", return_value=guard
            ), patch(
                "scheduled_runner.publish_plain_message", fake_publish_plain
            ):
                result = asyncio.run(sr.generate_and_publish_signal("aggressive", c, dry_run=False))

            self.assertTrue(result["published"])
            self.assertEqual(result["reason"], "published")
            self.assertTrue(result["aia_forward_attempted"])
            self.assertFalse(result["aia_forward_ok"])
            self.assertEqual(result["aia_forward_error"], "aia down")
            self.assertEqual(result["aia_forward_warning"], "aia_forward_failed")

    def test_run_signal_slot_logs_failed_aia_forward_as_publish_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            state = {"slots": {}}
            slot_time = sr.slot_datetime_msk(date(2026, 6, 5), "09:30")
            now = datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc)
            result = {
                "published": True,
                "reason": "published",
                "signal_id": "20260606_153001",
                "aia_forward_attempted": True,
                "aia_forward_ok": False,
                "aia_forward_error": "aia down",
                "aia_forward_mode": "awaited_scheduled",
                "aia_forward_warning": "aia_forward_failed",
            }
            with patch("scheduled_runner.LOGS_DIR", Path(tmpdir) / "logs"), patch(
                "scheduled_runner.load_aia_context", return_value={"status": "OPEN"}
            ), patch("scheduled_runner.generate_and_publish_signal", return_value=result):
                asyncio.run(sr.run_signal_slot(now, c, state, "20260605_0930", slot_time, 1, dry_run=False))

            rows = (Path(tmpdir) / "logs" / "scheduled_signal_decisions_20260605.jsonl").read_text(encoding="utf-8").splitlines()
            row = json.loads(rows[-1])
            self.assertEqual(row["decision"], "publish")
            self.assertEqual(row["signal_id"], "20260606_153001")
            self.assertTrue(row["aia_forward_attempted"])
            self.assertFalse(row["aia_forward_ok"])
            self.assertEqual(row["aia_forward_error"], "aia down")
            self.assertEqual(row["aia_forward_mode"], "awaited_scheduled")
            self.assertEqual(row["aia_forward_warning"], "aia_forward_failed")

    def test_hard_block_defers_then_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            state = {"slots": {}}
            slot_time = sr.slot_datetime_msk(date(2026, 6, 5), "09:30")
            gate_ctx = {
                "event_risk_level": "severe",
                "confirm_policy": "block_stale_confirm",
                "event_bias": "risk_off",
                "focus_asset": "SOL",
                "focus_direction": "LONG",
            }
            with patch("scheduled_runner.load_aia_context", return_value=gate_ctx):
                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc), c, state, "20260605_0930", slot_time, 1, dry_run=True))
                self.assertEqual(state["slots"]["20260605_0930"]["status"], "deferred")
                self.assertEqual(state["slots"]["20260605_0930"]["next_retry_at_msk"], "2026-06-05T10:30:00+03:00")

                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 7, 30, tzinfo=timezone.utc), c, state, "20260605_0930", slot_time, 2, dry_run=True))
                self.assertEqual(state["slots"]["20260605_0930"]["status"], "cancelled")

    def test_publish_then_duplicate_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            state = {"slots": {}}
            slot_time = sr.slot_datetime_msk(date(2026, 6, 5), "09:30")
            with patch("scheduled_runner.load_aia_context", return_value={"status": "OPEN"}), patch(
                "scheduled_runner.generate_and_publish_signal",
                return_value={"published": True, "reason": "published", "signal_id": "sig-1"},
            ):
                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc), c, state, "20260605_0930", slot_time, 1, dry_run=True))
                self.assertEqual(state["slots"]["20260605_0930"]["status"], "published")
                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 6, 31, tzinfo=timezone.utc), c, state, "20260605_0930", slot_time, 1, dry_run=True))
                self.assertEqual(state["slots"]["20260605_0930"]["signal_id"], "sig-1")

    def test_signal_core_no_trade_defers_or_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            c = cfg(Path(tmpdir))
            state = {"slots": {}}
            slot_time = sr.slot_datetime_msk(date(2026, 6, 5), "12:30")
            with patch(
                "scheduled_runner.load_aia_context",
                return_value={"status": "AVOID", "preferred_mode": "conservative"},
            ), patch(
                "scheduled_runner.generate_and_publish_signal",
                return_value={"published": False, "reason": "signal_core_no_trade", "signal_id": None},
            ) as publish:
                now = datetime(2026, 6, 5, 9, 30, tzinfo=timezone.utc)
                asyncio.run(sr.run_signal_slot(now, c, state, "20260605_1230", slot_time, 1, dry_run=True))
                publish.assert_called_with(
                    "aggressive",
                    c,
                    dry_run=True,
                    now_utc=now,
                    gate=sr.evaluate_aia_gate({"status": "AVOID", "preferred_mode": "conservative"}, c),
                )
                self.assertEqual(state["slots"]["20260605_1230"]["status"], "deferred")
                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 10, 30, tzinfo=timezone.utc), c, state, "20260605_1230", slot_time, 2, dry_run=True))
                self.assertEqual(state["slots"]["20260605_1230"]["status"], "cancelled")

    def test_log_rows_include_targets_and_aia_context(self) -> None:
        c = cfg()
        gate = sr.evaluate_aia_gate({"status": "AVOID", "preferred_mode": "neutral", "flow_bias": "mixed"}, c)
        row = sr.base_signal_log_row(
            datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc),
            c,
            "20260605_0930",
            sr.slot_datetime_msk(date(2026, 6, 5), "09:30"),
            1,
            gate,
        )
        self.assertEqual(row["target_chat_ids"], [-1003492385200, -1003493070625, -1003530482991])
        self.assertEqual(row["aia_status"], "AVOID")
        self.assertIn("soft_avoid_downgrade", row)
        self.assertIn("aia_avoid_soft_allowed", row)
        self.assertIn("hard_block_reasons", row)
        self.assertEqual(row["selected_mode"], "aggressive")
        self.assertEqual(row["selected_mode_source"], "scheduled_default_soft_no_hard_block")
        self.assertFalse(row["preferred_mode_downgrade_enabled"])
        self.assertEqual(row["preferred_mode_ignored_reason"], "soft_gate_no_hard_block")


if __name__ == "__main__":
    unittest.main()
