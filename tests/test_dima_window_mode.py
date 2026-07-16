from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import scheduled_runner as sr


def _cfg() -> sr.SchedulerConfig:
    root = Path(tempfile.gettempdir())
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
        signal_slots_msk=["09:30"],
        retry_delay_minutes=60,
        max_attempts=2,
        gate_mode="soft",
        preferred_mode_downgrade_enabled=False,
        soft_avoid_downgrade=False,
        signal_state_path=root / "scheduled_signal_state.json",
        publish_state_path=root / "scheduled_publish_state.json",
        due_window_minutes=5,
        dima_scheduled_mode="WINDOW_ONLY",
    )


def _avoid_context(**updates) -> dict:
    context = {
        "aia_status": "AVOID_HARD",
        "allowed": False,
        "focus_asset": "none",
        "focus_direction": "observe only",
        "event_risk_level": "severe",
        "event_bias": "risk_off",
        "confirm_policy": "block_stale_confirm",
        "dominant_critical_topic": "strategic_shipping_energy_chokepoint",
    }
    context.update(updates)
    return context


class DimaWindowModeTests(unittest.TestCase):
    def test_scheduled_full_signal_targets_exclude_only_dima(self) -> None:
        cfg = _cfg()
        self.assertEqual(sr.scheduled_full_signal_targets(cfg), [-1003492385200, -1003530482991])

        cfg.dima_scheduled_mode = "FULL_SIGNAL"
        self.assertEqual(sr.scheduled_full_signal_targets(cfg), cfg.target_chat_ids)

    def test_dima_scheduled_signal_is_replaced_by_non_lifecycle_window(self) -> None:
        cfg = _cfg()
        state = {}
        audit = asyncio.run(
            sr.maybe_publish_dima_market_window(
                cfg,
                state,
                _avoid_context(),
                now_utc=datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc),
                dry_run=True,
            )
        )
        message = sr.render_dima_market_window(_avoid_context())

        self.assertTrue(audit["window_message_sent"])
        self.assertFalse(audit["lifecycle_created"])
        self.assertEqual(audit["channel_mode"], "WINDOW_ONLY")
        self.assertIn("🧭 Рыночное окно", message)
        self.assertIn("сейчас плохое окно для нового входа", message)
        for forbidden in ("entry", " SL", " TP", " RR"):
            self.assertNotIn(forbidden, message)

    def test_avoid_hard_without_focus_is_concrete_russian_window(self) -> None:
        message = sr.render_dima_market_window(_avoid_context())
        self.assertIn("Фокус: нет", message)
        self.assertIn("не открывать новые позиции с рынка", message)
        self.assertIn("активен тяжёлый геополитический риск", message)
        self.assertNotIn("AVOID_HARD", message)
        self.assertNotIn("allowed for", message.lower())

    def test_severe_risk_off_short_focus_uses_retest_window(self) -> None:
        message = sr.render_dima_market_window(
            _avoid_context(
                aia_status="WATCH",
                allowed=True,
                focus_asset="BTC",
                focus_direction="SHORT",
            )
        )
        self.assertIn("Фокус: BTC/USDT", message)
        self.assertIn("шорт после подтверждения", message)
        self.assertIn("ждать ретест", message)
        self.assertIn("подтверждённого отбоя вниз", message)

    def test_severe_risk_off_long_is_caution_only(self) -> None:
        context = _avoid_context(
            aia_status="WATCH",
            allowed=True,
            focus_asset="BTC",
            focus_direction="LONG",
        )
        self.assertEqual(sr.classify_dima_window(context), "CAUTION_WINDOW")
        message = sr.render_dima_market_window(context)
        self.assertIn("LONG после отката и подтверждения", message)
        self.assertIn("фон остаётся хрупким", message)

    def test_avoid_hard_with_btc_long_background_risk_still_shows_retest_window(self) -> None:
        context = _avoid_context(
            aia_status="AVOID_HARD",
            allowed=False,
            focus_asset="BTC",
            focus_direction="LONG",
            flow_bias="bullish",
        )
        self.assertEqual(sr.classify_dima_window(context), "CAUTION_WINDOW")
        message = sr.render_dima_market_window(context)
        self.assertIn("Фокус: BTC/USDT", message)
        self.assertIn("LONG после отката и подтверждения", message)
        self.assertIn("ждать ретест EMA20 M15 или пробитого уровня", message)
        self.assertIn("не догонять импульс", message)

    def test_post_generation_tactical_context_reaches_dima_renderer(self) -> None:
        gate = _avoid_context(
            aia_status="AVOID_HARD",
            allowed=True,
            focus_asset="none",
            focus_direction="observe only",
            flow_bias="bullish",
        )
        result = {
            "execution_mode": "TACTICAL_CONFIRM_ONLY",
            "risk_size_mode": "REDUCED",
            "entry_mode_required": "WAIT_CONFIRM",
            "last_payload": {
                "symbol": "BTC/USDT",
                "direction": "long",
                "entry_mode": "wait_confirm",
                "extended_breakout": True,
            },
        }

        context = sr._dima_context_from_signal_result(gate, result)
        self.assertEqual(context["focus_asset"], "BTC/USDT")
        self.assertEqual(context["focus_direction"], "long")
        self.assertEqual(context["execution_mode"], "TACTICAL_CONFIRM_ONLY")

        message = sr.render_dima_market_window(context)
        self.assertIn("Фокус: BTC/USDT", message)
        self.assertIn("LONG после отката и подтверждения", message)
        self.assertIn("рынок сохраняет бычью структуру", message)

    def test_execution_mode_change_bypasses_generic_window_cooldown(self) -> None:
        cfg = _cfg()
        state = {}
        now = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
        asyncio.run(
            sr.maybe_publish_dima_market_window(
                cfg,
                state,
                _avoid_context(
                    aia_status="WATCH",
                    allowed=True,
                    focus_asset="BTC",
                    focus_direction="LONG",
                    execution_mode="NORMAL",
                ),
                now_utc=now,
                dry_run=True,
            )
        )
        changed = asyncio.run(
            sr.maybe_publish_dima_market_window(
                cfg,
                state,
                _avoid_context(
                    aia_status="WATCH",
                    allowed=True,
                    focus_asset="BTC",
                    focus_direction="LONG",
                    execution_mode="TACTICAL_CONFIRM_ONLY",
                ),
                now_utc=now + timedelta(minutes=10),
                dry_run=True,
            )
        )

        self.assertTrue(changed["window_message_sent"])
        self.assertFalse(changed["dima_window_cooldown_applied"])
        self.assertEqual(changed["dima_window_audit_reason"], "execution_mode_changed")

    def test_repeated_same_window_is_suppressed_during_cooldown(self) -> None:
        cfg = _cfg()
        state = {}
        now = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
        first = asyncio.run(sr.maybe_publish_dima_market_window(cfg, state, _avoid_context(), now_utc=now, dry_run=True))
        second = asyncio.run(
            sr.maybe_publish_dima_market_window(
                cfg,
                state,
                _avoid_context(),
                now_utc=now + timedelta(minutes=30),
                dry_run=True,
            )
        )
        self.assertTrue(first["window_message_sent"])
        self.assertFalse(second["window_message_sent"])
        self.assertTrue(second["dima_window_cooldown_applied"])

    def test_risk_regime_change_bypasses_cooldown(self) -> None:
        cfg = _cfg()
        state = {}
        now = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
        asyncio.run(sr.maybe_publish_dima_market_window(cfg, state, _avoid_context(), now_utc=now, dry_run=True))
        changed = asyncio.run(
            sr.maybe_publish_dima_market_window(
                cfg,
                state,
                _avoid_context(event_bias="risk_on", event_risk_level="medium"),
                now_utc=now + timedelta(minutes=10),
                dry_run=True,
            )
        )
        self.assertTrue(changed["window_message_sent"])
        self.assertFalse(changed["dima_window_cooldown_applied"])
        self.assertEqual(changed["dima_window_update_reason"], "изменился режим риска")

    def test_stale_dima_macro_window_is_suppressed(self) -> None:
        cfg = _cfg()
        now = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)
        audit = asyncio.run(
            sr.maybe_publish_dima_macro_window(
                cfg,
                {"event_name": "Fed Bowman speech", "event_time_utc": "2026-07-13T16:30:00Z"},
                None,
                now_utc=now,
                dry_run=True,
            )
        )
        self.assertFalse(audit["dima_macro_window_sent"])
        self.assertTrue(audit["dima_macro_stale_suppressed"])


if __name__ == "__main__":
    unittest.main()
