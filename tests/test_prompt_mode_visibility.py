import unittest

import get_signal_json


class TestPromptModeVisibility(unittest.TestCase):
    def test_cli_code_contains_mode_line_single_and_multi(self) -> None:
        code = get_signal_json._CLI_CODE
        self.assertIn(
            "MODE: {mode}. Follow the MODE-SPECIFIC DECISION CONTRACT below.",
            code,
        )

    def test_cli_code_injects_mode_and_requested_mode_into_payloads(self) -> None:
        code = get_signal_json._CLI_CODE

        # MULTI/FULL payload JSON.
        self.assertIn('"mode": mode', code)
        self.assertIn('pool_payload["requested_mode"] = requested_mode', code)

        # SINGLE payload JSON.
        self.assertIn('payload["mode"] = mode', code)
        self.assertIn('payload["requested_mode"] = normalize_mode(mode_raw)', code)


if __name__ == "__main__":
    unittest.main()

