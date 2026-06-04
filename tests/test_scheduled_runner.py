import asyncio
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
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
    def test_open_without_hard_block_allows_publish(self) -> None:
        out = sr.evaluate_aia_gate({"status": "OPEN", "preferred_mode": "aggressive"}, cfg())
        self.assertTrue(out["allowed"])
        self.assertEqual(out["selected_mode"], "aggressive")

    def test_soft_avoid_without_hard_block_allows_and_downgrades_to_neutral(self) -> None:
        out = sr.evaluate_aia_gate(
            {"status": "AVOID", "preferred_mode": "conservative", "focus_asset": "BTC", "focus_direction": "SHORT"},
            cfg(),
        )
        self.assertTrue(out["allowed"])
        self.assertTrue(out["aia_avoid_soft_allowed"])
        self.assertEqual(out["selected_mode"], "neutral")
        self.assertEqual(out["reason"], "avoid_without_hard_block")

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


class TestScheduledSignalState(unittest.TestCase):
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
            with patch("scheduled_runner.load_aia_context", return_value={"status": "OPEN"}), patch(
                "scheduled_runner.generate_and_publish_signal",
                return_value={"published": False, "reason": "signal_core_no_trade", "signal_id": None},
            ):
                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 9, 30, tzinfo=timezone.utc), c, state, "20260605_1230", slot_time, 1, dry_run=True))
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
        self.assertIn("hard_block_reasons", row)


if __name__ == "__main__":
    unittest.main()
