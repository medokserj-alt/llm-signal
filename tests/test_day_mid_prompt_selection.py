import unittest
from pathlib import Path

import get_signal_json


BASE = Path(__file__).resolve().parent.parent


class TestDayMidPromptSelection(unittest.TestCase):
    def test_cli_code_accepts_explicit_analysis_prompt_file(self) -> None:
        code = get_signal_json._CLI_CODE
        self.assertIn('ap.add_argument("--analysis-prompt", default="prompt_analysis.txt")', code)
        self.assertIn("system_prompt = read_file(args.analysis_prompt)", code)

    def test_resolve_event_calendar_profile_uses_profile_specific_prompt_text(self) -> None:
        day_prompt = (BASE / "prompt_day.txt").read_text(encoding="utf-8")
        mid_prompt = (BASE / "prompt_mid.txt").read_text(encoding="utf-8")

        self.assertEqual(get_signal_json.resolve_event_calendar_profile(system_prompt=day_prompt), "day")
        self.assertEqual(get_signal_json.resolve_event_calendar_profile(system_prompt=mid_prompt), "mid")

    def test_compose_overview_sections_is_profile_specific(self) -> None:
        overview_lines = ["Header", "Drivers", "Scenarios"]
        flow_section = "Flow overlay"

        day_sections = get_signal_json._compose_overview_sections(
            overview_lines,
            analysis_profile="day",
            flow_derivatives_section=flow_section,
        )
        mid_sections = get_signal_json._compose_overview_sections(
            overview_lines,
            analysis_profile="mid",
            flow_derivatives_section=flow_section,
        )

        self.assertEqual(day_sections, ["Header\n\nDrivers\n\nScenarios"])
        self.assertEqual(mid_sections, ["Header", "Flow overlay", "Drivers\n\nScenarios"])


if __name__ == "__main__":
    unittest.main()
