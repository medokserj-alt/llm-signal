import get_signal_json


def _base_signal(direction: str = "long", symbol: str = "BNB/USDT") -> dict:
    return {
        "time_msk": "01.06.2026, 13:06",
        "symbol": symbol,
        "price": 700.0,
        "direction": direction,
        "entry_range": [697.0, 702.0],
        "sl": 692.0 if direction == "long" else 73200.0,
        "tp1": 710.0 if direction == "long" else 71350.0,
        "tp2": 716.0 if direction == "long" else 70987.0,
        "rr": 2.0,
        "entry_mode": "limit",
        "mode": "aggressive",
        "confidence": "High",
        "no_trade": False,
        "ema20_m15": 700.0,
        "ema20_h1": 701.0,
        "price_vs_ema20_m15": "above" if direction == "long" else "below",
        "price_vs_ema20_h1": "above" if direction == "long" else "below",
        "event_risk_context": [],
        "critical_topics": [
            {
                "topic_id": "strategic_shipping_energy_chokepoint",
                "severity_floor": "severe",
                "event_bias": "risk_off",
                "headline_risk_active": True,
                "confirm_policy": "block_stale_confirm",
                "matched_entities": ["hormuz"],
                "matched_phrases": ["blockade"],
                "escalation_reason": "strategic chokepoint risk",
            }
        ],
        "dominant_critical_topic": {
            "topic_id": "strategic_shipping_energy_chokepoint",
            "severity_floor": "severe",
            "event_bias": "risk_off",
            "headline_risk_active": True,
            "confirm_policy": "block_stale_confirm",
        },
        "headline_risk_active": True,
        "event_bias": "risk_off",
        "confirm_policy": "block_stale_confirm",
    }


def test_bnb_aggressive_alt_long_under_severe_risk_off_is_no_trade_without_reclaim() -> None:
    out = _base_signal()
    out.setdefault("warnings", [])
    out.setdefault("no_trade_reasons", [])
    get_signal_json.apply_critical_topic_risk_compatibility(out)

    assert out["no_trade"] is True
    assert out["entry_mode"] == "wait_confirm"
    assert "critical_topic_risk_off_conflicts_with_alt_long" in out["no_trade_reasons"]
    assert out["execution_diagnosis"]["stale_risk_on_long_blocked"] is True


def test_btc_short_under_risk_off_alarm_can_remain_allowed_wait_confirm() -> None:
    data = _base_signal(direction="short", symbol="BTC/USDT")
    data["price"] = 72400.0
    data["entry_range"] = [72400.0, 72500.0]
    data["ema20_m15"] = 72800.0
    data["ema20_h1"] = 73000.0
    out = data
    out.setdefault("warnings", [])
    out.setdefault("no_trade_reasons", [])
    get_signal_json.apply_critical_topic_risk_compatibility(out)

    assert out["no_trade"] is False
    assert out["entry_mode"] == "wait_confirm"
    assert out["risk_compatibility"] == "aligned"
    assert "critical_topic_direction_aligned_wait_confirm" in out["warnings"]
    assert "severe_event_aligned_direction_strict_confirm_only" in out["warnings"]


def test_risk_on_btc_long_can_publish_with_strict_confirm() -> None:
    out = _base_signal(direction="long", symbol="BTC/USDT")
    out["event_bias"] = "risk_on"
    out["critical_topics"][0]["event_bias"] = "risk_on"
    out["dominant_critical_topic"]["event_bias"] = "risk_on"
    out.setdefault("warnings", [])
    out.setdefault("no_trade_reasons", [])
    get_signal_json.apply_critical_topic_risk_compatibility(out)

    assert out["no_trade"] is False
    assert out["entry_mode"] == "wait_confirm"
    assert out["direction_event_compatibility"] == "aligned"
    assert out["confirm_profile_used"] == "risk_on_long_confirm"
    assert "severe_event_aligned_direction_strict_confirm_only" in out["warnings"]


def test_risk_on_short_without_breakdown_is_no_trade() -> None:
    out = _base_signal(direction="short", symbol="BTC/USDT")
    out["event_bias"] = "risk_on"
    out["critical_topics"][0]["event_bias"] = "risk_on"
    out["dominant_critical_topic"]["event_bias"] = "risk_on"
    out["technical_rationale"] = "Weak fade idea near range; structure is mixed and unconfirmed."
    out.setdefault("warnings", [])
    out.setdefault("no_trade_reasons", [])
    get_signal_json.apply_critical_topic_risk_compatibility(out)

    assert out["no_trade"] is True
    assert out["entry_mode"] == "wait_confirm"
    assert out["direction_event_compatibility"] == "conflicting"
    assert "counter_regime_short_requires_confirmed_breakdown" in out["no_trade_reasons"]


def test_mixed_regime_low_liquidity_alt_is_no_trade() -> None:
    out = _base_signal(direction="long", symbol="DOGE/USDT")
    out["critical_topics"] = []
    out["dominant_critical_topic"] = {}
    out["event_bias"] = "mixed"
    out["event_risk_regime"] = {"severity": "severe", "directional_risk": "mixed"}
    out.setdefault("warnings", [])
    out.setdefault("no_trade_reasons", [])
    get_signal_json.apply_critical_topic_risk_compatibility(out)

    assert out["no_trade"] is True
    assert out["entry_mode"] == "wait_confirm"
    assert "mixed_regime_low_liquidity_alt_requires_clean_major" in out["no_trade_reasons"]
