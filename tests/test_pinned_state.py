import json
import tempfile
import unittest
from pathlib import Path

from pinned_state import get_pinned_message_id, load_pinned_state, save_pinned_state, set_pinned_message_id


class TestPinnedState(unittest.TestCase):
    def test_load_missing_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pinned_state.json"
            self.assertEqual(load_pinned_state(p), {})

    def test_roundtrip_and_deterministic_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pinned_state.json"
            st = {}

            st = set_pinned_message_id(st, -1001, "day", 111)
            st = set_pinned_message_id(st, -1001, "mid", 222)
            st = set_pinned_message_id(st, "-1002", "day", 333)

            save_pinned_state(p, st)
            st2 = load_pinned_state(p)

            self.assertEqual(get_pinned_message_id(st2, -1001, "day"), 111)
            self.assertEqual(get_pinned_message_id(st2, -1001, "mid"), 222)
            self.assertEqual(get_pinned_message_id(st2, -1002, "day"), 333)
            self.assertIsNone(get_pinned_message_id(st2, -1002, "mid"))

            # Verify file is valid JSON and stable keys are preserved.
            raw = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("-1001", raw)
            self.assertIn("-1002", raw)


if __name__ == "__main__":
    unittest.main()

