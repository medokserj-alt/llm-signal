import unittest
from pathlib import Path


BASE = Path(__file__).resolve().parent.parent


class TestDayMidReportScripts(unittest.TestCase):
    def test_run_day_uses_day_prompt_without_mutating_shared_prompt_file(self) -> None:
        text = (BASE / "run_day.sh").read_text(encoding="utf-8")

        self.assertIn('PROMPT_FILE="prompt_day.txt"', text)
        self.assertIn('./signal full --analysis-prompt "$PROMPT_FILE"', text)
        self.assertNotIn("cp -f prompt_analysis.txt prompt_analysis.bak", text)
        self.assertNotIn("cp -f prompt_day.txt      prompt_analysis.txt", text)
        self.assertNotIn("mv -f prompt_analysis.bak prompt_analysis.txt", text)

    def test_run_mid_uses_mid_prompt_without_mutating_shared_prompt_file(self) -> None:
        text = (BASE / "run_mid.sh").read_text(encoding="utf-8")

        self.assertIn('PROMPT_FILE="prompt_mid.txt"', text)
        self.assertIn('./signal full --analysis-prompt "$PROMPT_FILE"', text)
        self.assertNotIn("cp -f prompt_analysis.txt prompt_analysis.bak", text)
        self.assertNotIn("cp -f prompt_mid.txt      prompt_analysis.txt", text)
        self.assertNotIn("mv -f prompt_analysis.bak prompt_analysis.txt", text)

    def test_run_scripts_collect_only_fresh_run_specific_artifacts(self) -> None:
        for name in ("run_day.sh", "run_mid.sh"):
            text = (BASE / name).read_text(encoding="utf-8")
            self.assertNotIn('cp -f analysis_*.md', text)
            self.assertNotIn('cp -f logs/last.json', text)
            self.assertIn('-newer "$MARKER"', text)
            self.assertIn("last_[0-9]*.json", text)
            self.assertIn('cp -f "$run_last_json" "$STAGING/last.json"', text)


if __name__ == "__main__":
    unittest.main()
