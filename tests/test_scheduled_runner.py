import asyncio
import inspect
import json
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
        self.assertEqual(out["selected_mode"], "neutral")
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
        self.assertEqual(out["selected_mode"], "neutral")
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
        self.assertIn("severe_block_stale_confirm_conflicts_event_bias", out["hard_block_reasons"])

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
    async def _inline_to_thread(self, func, *args, **kwargs):
        return func(*args, **kwargs)

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

            async def fake_publish(*args, **kwargs):
                publish_kwargs.append(kwargs)
                return True

            c = cfg(Path(tmpdir))
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

            async def fake_publish(*args, **kwargs):
                return True

            c = cfg(Path(tmpdir))
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
                asyncio.run(sr.run_signal_slot(datetime(2026, 6, 5, 9, 30, tzinfo=timezone.utc), c, state, "20260605_1230", slot_time, 1, dry_run=True))
                publish.assert_called_with("aggressive", c, dry_run=True)
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
