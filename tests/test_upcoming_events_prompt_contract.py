import unittest
from pathlib import Path


BASE = Path(__file__).resolve().parent.parent


class TestUpcomingEventsPromptContract(unittest.TestCase):
    def test_day_prompt_requires_structured_forward_risk_fallback(self) -> None:
        text = (BASE / "prompt_day.txt").read_text(encoding="utf-8")

        self.assertIn('"upcoming_events": object[]', text)
        self.assertIn("`upcoming_events` is REQUIRED in every output and must NEVER be null.", text)
        self.assertIn("Prefer structured representation of risk over narrative-only description.", text)
        self.assertIn("Fallback forward-risk buckets when classic macro is absent:", text)
        self.assertIn("market structure risks: thin liquidity sessions, liquidation clusters/zones, options expiry, funding/reset windows", text)
        self.assertIn('Returning `upcoming_events`: [] is allowed ONLY if there are truly no identifiable forward risks', text)
        self.assertIn('`time_msk: "TBD"`', text)
        self.assertIn("calendar_events: structured scheduled events from local event calendar; primary source of scheduled event timing", text)
        self.assertIn("If `calendar_events` contains a valid scheduled item inside the horizon, it MUST appear in `upcoming_events`.", text)
        self.assertIn("If `calendar_events` is empty, do not invent scheduled events.", text)
        self.assertIn("`🗓 Ключевые события периода`", text)

    def test_mid_prompt_requires_structured_forward_risk_fallback(self) -> None:
        text = (BASE / "prompt_mid.txt").read_text(encoding="utf-8")

        self.assertIn("`upcoming_events` и `macro_risk_summary` обязательны всегда.", text)
        self.assertIn("`upcoming_events` is REQUIRED in every output and must NEVER be null.", text)
        self.assertIn("Prefer structured representation of risk over narrative-only description.", text)
        self.assertIn("Fallback forward-risk buckets when classic macro is absent:", text)
        self.assertIn("market structure risks: thin liquidity periods, liquidation clusters/zones, options expiry, funding/reset windows", text)
        self.assertIn('Returning `upcoming_events`: [] is allowed ONLY if there are truly no identifiable forward risks', text)
        self.assertIn('`time_msk: "TBD"`', text)
        self.assertIn("calendar_events: structured scheduled events from local event calendar; primary source of scheduled event timing", text)
        self.assertIn("If `calendar_events` contains a valid scheduled item inside the horizon, it MUST appear in `upcoming_events`.", text)
        self.assertIn("If `calendar_events` is empty, do not invent scheduled events.", text)
        self.assertIn("`🗓 Ключевые события периода`", text)


if __name__ == "__main__":
    unittest.main()
