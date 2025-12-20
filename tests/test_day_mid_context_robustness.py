import unittest


from postprocess import process as postprocess_process


class TestDayMidContextRobustness(unittest.TestCase):
    def test_day_mid_context_string_does_not_crash(self) -> None:
        d = {"day_mid_context": "Context from LLM (string)"}
        out = postprocess_process(d, day_txt=None, mid_txt=None)
        ctx = out.get("day_mid_context")
        self.assertIsInstance(ctx, dict)
        self.assertIn("day_bias", ctx)
        self.assertIn("mid_bias", ctx)
        self.assertIn("notes", ctx)
        self.assertEqual(ctx.get("notes"), "Context from LLM (string)")

    def test_day_mid_context_none_does_not_crash(self) -> None:
        d = {"day_mid_context": None}
        out = postprocess_process(d, day_txt=None, mid_txt=None)
        ctx = out.get("day_mid_context")
        self.assertIsInstance(ctx, dict)
        self.assertIn("day_bias", ctx)
        self.assertIn("mid_bias", ctx)
        self.assertIn("notes", ctx)


if __name__ == "__main__":
    unittest.main()

