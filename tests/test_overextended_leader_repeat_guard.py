import unittest

import get_signal_json
import no_trade_explain


class OverextendedLeaderRepeatGuardTest(unittest.TestCase):
    def setUp(self):
        self._old_log_count = get_signal_json._recent_no_confirm_count_from_logs
        get_signal_json._recent_no_confirm_count_from_logs = lambda _symbol, _side: 0

    def tearDown(self):
        get_signal_json._recent_no_confirm_count_from_logs = self._old_log_count

    def _base_signal(self, **overrides):
        d = {
            "symbol": "BNB/USDT",
            "side": "long",
            "direction": "long",
            "mode": "aggressive",
            "no_trade": False,
            "no_trade_reasons": [],
            "no_trade_hint": "",
            "warnings": [],
            "pool_quotes": {
                "BTC/USDT": {"change": 1.0},
                "ETH/USDT": {"change": 1.5},
                "BNB/USDT": {"change": 8.0},
                "SOL/USDT": {"change": 2.0},
                "XRP/USDT": {"change": 0.5},
            },
            "price_vs_ema20_m15": "below",
            "price_vs_ema20_h1": "above",
            "ema_fan_m15_state": "mixed",
            "recent_signal_attempts": [
                {"symbol": "BNB/USDT", "direction": "long", "status": "WAIT_CONFIRM"},
                {"symbol": "BNB/USDT", "direction": "long", "status": "INVALIDATED_NO_CONFIRM"},
            ],
        }
        d.update(overrides)
        return d

    def test_bnb_like_overextended_repeat_no_confirm_downgrades_to_no_trade(self):
        d = self._base_signal()

        get_signal_json.apply_overextended_leader_repeat_guard(d)

        self.assertTrue(d["overextended_leader_risk"])
        self.assertTrue(d["no_trade"])
        self.assertIn(
            get_signal_json.OVEREXTENDED_LEADER_NO_TRADE_REASON,
            d["no_trade_reasons"],
        )
        self.assertEqual(d["same_asset_direction_recent_no_confirm_count"], 2)
        self.assertIn("требует reset/reclaim", d["no_trade_hint"])
        rendered = no_trade_explain.format_no_trade_message(d)
        self.assertIn("Не догоняем прежний импульс", rendered)
        self.assertTrue(d["execution_diagnosis"]["overextended_leader_repeat_long"])
        self.assertTrue(d["execution_diagnosis"]["requires_reset_reclaim"])

    def test_clean_leader_early_trend_still_allowed(self):
        d = self._base_signal(
            pool_quotes={
                "BTC/USDT": {"change": 1.0},
                "ETH/USDT": {"change": 1.5},
                "BNB/USDT": {"change": 3.0},
                "SOL/USDT": {"change": 2.0},
            },
            price_vs_ema20_m15="above",
            price_vs_ema20_h1="above",
            ema_fan_m15_state="bull",
            ema_fan_h1_state="bull",
            recent_signal_attempts=[],
        )

        get_signal_json.apply_overextended_leader_repeat_guard(d)

        self.assertFalse(d.get("overextended_leader_risk"))
        self.assertFalse(d["no_trade"])
        self.assertNotIn(get_signal_json.OVEREXTENDED_LEADER_WARNING, d["warnings"])

    def test_overextended_leader_with_fresh_reclaim_allowed_with_warning(self):
        d = self._base_signal(
            price_vs_ema20_m15="above",
            price_vs_ema20_h1="above",
            ema_fan_m15_state="mixed",
            fresh_reset_reclaim=True,
        )

        get_signal_json.apply_overextended_leader_repeat_guard(d)

        self.assertTrue(d["overextended_leader_risk"])
        self.assertFalse(d["no_trade"])
        self.assertIn(get_signal_json.OVEREXTENDED_LEADER_WARNING, d["warnings"])
        self.assertTrue(d["execution_diagnosis"]["requires_reset_reclaim"])

    def test_other_assets_not_affected_when_not_top_leader(self):
        d = self._base_signal(
            symbol="SOL/USDT",
            pool_quotes={
                "BTC/USDT": {"change": 1.0},
                "ETH/USDT": {"change": 1.5},
                "BNB/USDT": {"change": 8.0},
                "SOL/USDT": {"change": 2.0},
            },
            recent_signal_attempts=[
                {"symbol": "SOL/USDT", "direction": "long", "status": "WAIT_CONFIRM"},
                {"symbol": "SOL/USDT", "direction": "long", "status": "EXPIRED_NO_CONFIRM"},
            ],
        )

        get_signal_json.apply_overextended_leader_repeat_guard(d)

        self.assertFalse(d.get("overextended_leader_risk"))
        self.assertFalse(d["no_trade"])

    def test_short_candidates_not_affected(self):
        d = self._base_signal(side="short", direction="short")

        get_signal_json.apply_overextended_leader_repeat_guard(d)

        self.assertNotIn("overextended_leader_risk", d)
        self.assertFalse(d["no_trade"])


if __name__ == "__main__":
    unittest.main()
