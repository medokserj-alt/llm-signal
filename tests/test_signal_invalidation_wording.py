import unittest

import get_signal_json


class TestSignalInvalidationWording(unittest.TestCase):
    def test_long_invalidation_wording_uses_below_and_structural_low(self) -> None:
        d = {
            "side": "long",
            "why_asset": "чёткий invalidate (уход выше EMA60 M15/структурного лоя)",
        }

        get_signal_json._sanitize_signal_invalidation_wording(d)

        self.assertEqual(
            d.get("why_asset"),
            "отмена сценария: уход ниже EMA60(M15) / структурного low",
        )

    def test_short_invalidation_wording_uses_above_and_structural_high(self) -> None:
        d = {
            "side": "short",
            "technical_rationale": {
                "summary": "invalidate: уход ниже EMA60(M15) / structural high",
            },
        }

        get_signal_json._sanitize_signal_invalidation_wording(d)

        self.assertEqual(
            (d.get("technical_rationale") or {}).get("summary"),
            "отмена сценария: уход выше EMA60(M15) / структурного high",
        )


if __name__ == "__main__":
    unittest.main()
