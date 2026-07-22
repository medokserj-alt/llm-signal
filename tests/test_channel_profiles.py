import os
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import channel_profiles
import scheduled_runner
import tg_bot


class ChannelProfileRoutingTests(unittest.TestCase):
    ENV = {
        "V_CHAT_ID": "-1003907734859",
        "V_TELEGRAM_USER_ID": "8718833768",
        "TG_DIMA_CHAT_ID": "-1003493070625",
        "TG_DIMA_TELEGRAM_USER_ID": "8556231754",
    }

    def test_v_and_dima_profile_permissions(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            v = channel_profiles.profile_for_user(8718833768)
            dima = channel_profiles.profile_for_user(8556231754)
            self.assertTrue(v.receive_scheduled_signals)
            self.assertFalse(dima.receive_scheduled_signals)
            self.assertTrue(v.receive_day and v.receive_mid)
            self.assertTrue(dima.receive_day and dima.receive_mid)

    def test_scheduled_targets_add_v_and_always_exclude_dima(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            cfg = scheduled_runner.SchedulerConfig.from_env()
            cfg.target_chat_ids = [-1003492385200, -1003493070625, -1003530482991]
            self.assertEqual(
                scheduled_runner.scheduled_full_signal_targets(cfg),
                [-1003492385200, -1003530482991, -1003907734859],
            )
            cfg.dima_scheduled_mode = "FULL_SIGNAL"
            self.assertNotIn(-1003493070625, scheduled_runner.scheduled_full_signal_targets(cfg))

    def test_day_mid_targets_include_both_profiles(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            self.assertEqual(
                channel_profiles.report_end_user_chat_ids("day"),
                [-1003907734859, -1003493070625],
            )
            self.assertEqual(
                channel_profiles.report_end_user_chat_ids("mid"),
                [-1003907734859, -1003493070625],
            )
            cfg = scheduled_runner.SchedulerConfig.from_env()
            cfg.target_chat_ids = [-1003492385200, -1003493070625, -1003530482991]
            self.assertEqual(
                scheduled_runner.scheduled_report_targets("day", cfg),
                [-1003492385200, -1003493070625, -1003530482991, -1003907734859],
            )

    def test_v_missing_chat_fails_safe(self):
        env = dict(self.ENV)
        env.pop("V_CHAT_ID")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn(-1003907734859, channel_profiles.scheduled_end_user_chat_ids())

    def test_v_requested_signal_persists_structured_owner(self):
        payload = {
            "symbol": "BTC/USDT",
            "direction": "long",
            "entry_range": [100, 101],
            "sl": 99,
            "tp1": 103,
            "tp2": 105,
            "uid": 8718833768,
            "mode": "neutral",
        }
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            out = tg_bot._build_signal_json_v1(
                signal_id="request-v-1",
                published_at="2026-07-22T12:00:00Z",
                channel_id=-1003907734859,
                origin_chat_id=-1003907734859,
                publish_targets=[-1003907734859],
                last_payload=payload,
            )
        self.assertEqual(out["signal_origin_type"], "USER_REQUESTED")
        self.assertEqual(out["requester_telegram_user_id"], 8718833768)
        self.assertEqual(out["requester_profile_id"], "END_USER_V")
        self.assertEqual(out["owner_profile_id"], "END_USER_V")
        self.assertEqual(out["request_correlation_id"], "request-v-1")

    def test_day_retry_sends_only_missing_target(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, self.ENV, clear=False):
            root = Path(tmpdir)
            cfg = scheduled_runner.SchedulerConfig.from_env()
            cfg.target_chat_ids = [-1003492385200, -1003493070625, -1003530482991]
            cfg.publish_state_path = root / "publish-state.json"
            artifact = root / "reports" / "day" / "stable"
            artifact.mkdir(parents=True)
            now = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
            cfg.publish_state_path.write_text(
                json.dumps(
                    {
                        "day": {
                            "20260722": {
                                "status": "partial",
                                "artifact_path": str(artifact),
                                "target_chat_ids": [-1003492385200, -1003493070625, -1003530482991],
                            }
                        },
                        "mid": {},
                    }
                ),
                encoding="utf-8",
            )
            async_publish = mock.AsyncMock(
                return_value={
                    "artifact_path": str(artifact),
                    "message_id": None,
                    "target_chat_ids": [-1003907734859],
                    "target_errors": {},
                }
            )
            with mock.patch.object(scheduled_runner, "publish_report", async_publish), mock.patch.object(
                scheduled_runner, "append_jsonl"
            ):
                asyncio.run(scheduled_runner.run_publish_job("day", now, cfg))
            self.assertEqual(async_publish.await_args.kwargs["target_chat_ids"], [-1003907734859])
            state = json.loads(cfg.publish_state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["day"]["20260722"]["status"], "published")
            self.assertEqual(len(state["day"]["20260722"]["target_chat_ids"]), 4)


if __name__ == "__main__":
    unittest.main()
