import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from event_calendar import read_event_calendar
from tools import update_event_calendar


NOW_UTC = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
BLS_EMPLOYMENT_HTML = """
<html>
  <body>
    <table>
      <tr>
        <th>Reference Month</th>
        <th>Release Date</th>
        <th>Release Time</th>
      </tr>
      <tr>
        <td>March 2026</td>
        <td>Apr. 03, 2026</td>
        <td>08:30 AM</td>
      </tr>
    </table>
  </body>
</html>
"""
BLS_CPI_HTML = """
<html>
  <body>
    <table>
      <tr>
        <th>Reference Month</th>
        <th>Release Date</th>
        <th>Release Time</th>
      </tr>
      <tr>
        <td>March 2026</td>
        <td>Apr. 10, 2026</td>
        <td>08:30 AM</td>
      </tr>
    </table>
  </body>
</html>
"""
BEA_RELEASE_DATES = {
    "Gross Domestic Product": {
        "release_dates": [
            "2026-04-30T12:30:00+00:00",
        ]
    },
    "Personal Income and Outlays": {
        "release_dates": [
            "2026-04-30T12:30:00+00:00",
        ]
    },
}
FED_FOMC_HTML = """
<html>
  <body>
    <h4>2026 FOMC Meetings</h4>
    <p>March</p>
    <p>17-18*</p>
    <p>April</p>
    <p>28-29</p>
    <p>June</p>
    <p>16-17*</p>
  </body>
</html>
"""
FED_CALENDAR_INDEX_HTML = """
<html>
  <body>
    <a href="/newsevents/2026-april.htm">April 2026</a>
    <a href="/newsevents/2026-may.htm">May 2026</a>
  </body>
</html>
"""
FED_APRIL_HTML = """
<html>
  <body>
    <h3>April 2026</h3>
    <h4>Speeches</h4>
    <p>Time:</p>
    <p>Release Date(s):</p>
    <p>9:00 a.m.</p>
    <p>Speech - Chair Jerome H. Powell</p>
    <p>Economic outlook remarks</p>
    <p>At the April Policy Forum</p>
    <p>4</p>
    <h4>FOMC Meetings</h4>
    <p>Time:</p>
    <p>Release Date(s):</p>
    <p>2:00 p.m.</p>
    <p>FOMC Minutes</p>
    <p>Meeting of March 17-18</p>
    <p>8</p>
    <p>2:30 p.m.</p>
    <p>FOMC Press Conference</p>
    <p>29</p>
    <p>2:00 p.m.</p>
    <p>FOMC Meeting</p>
    <p>Two-day meeting, April 28 - 29</p>
    <p>Press Conference</p>
    <p>29</p>
  </body>
</html>
"""
FED_MAY_HTML = """
<html>
  <body>
    <h3>May 2026</h3>
    <h4>Other</h4>
  </body>
</html>
"""


class TestEventCalendarUpdater(unittest.TestCase):
    def _assert_schema_valid_event(self, item: dict) -> None:
        self.assertIsInstance(item, dict)
        self.assertIn("time_msk", item)
        self.assertIn("event", item)
        self.assertIn("category", item)
        self.assertIn("impact", item)
        self.assertIn("window_before_min", item)
        self.assertIn("window_after_min", item)
        self.assertIn("source", item)
        self.assertIn("note", item)
        self.assertIn(item["category"], {"macro", "fed", "politics", "crypto"})
        self.assertIn(item["impact"], {"high", "medium", "low"})
        self.assertIsInstance(item["window_before_min"], int)
        self.assertIsInstance(item["window_after_min"], int)

    def test_parse_bls_schedule_html_returns_normalized_event(self) -> None:
        events = update_event_calendar._parse_bls_schedule_html(
            BLS_CPI_HTML,
            event="US CPI",
            category="macro",
            impact="high",
            note="BLS Consumer Price Index release.",
            priority=80,
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event"], "US CPI")
        self.assertEqual(event["category"], "macro")
        self.assertEqual(event["impact"], "high")
        self.assertEqual(event["time_msk"], "10.04.2026, 15:30")
        self.assertEqual(event["date_msk"], "10.04.2026")
        self.assertIn("Reference period: March 2026.", event["note"])

    def test_parse_bea_release_dates_json_returns_target_macro_events(self) -> None:
        events = update_event_calendar._parse_bea_release_dates_json(BEA_RELEASE_DATES)

        self.assertEqual({item["event"] for item in events}, {"US GDP", "US Personal Income and Outlays (PCE)"})
        for event in events:
            self.assertEqual(event["date_msk"], "30.04.2026")
            self.assertEqual(event["time_msk"], "30.04.2026, 15:30")
            self.assertEqual(event["category"], "macro")
            self.assertEqual(event["impact"], "high")

    def test_fed_parsers_dedupe_meeting_and_keep_scheduled_events(self) -> None:
        events = update_event_calendar._dedupe_events(
            update_event_calendar._parse_fed_fomc_calendar_html(FED_FOMC_HTML)
            + update_event_calendar._parse_fed_calendar_month_html(FED_APRIL_HTML)
        )

        by_name_and_date = {(item["event"], item.get("date_msk")): item for item in events}
        self.assertEqual(by_name_and_date[("Jerome Powell speech", "04.04.2026")]["time_msk"], "04.04.2026, 16:00")
        self.assertEqual(by_name_and_date[("FOMC Minutes", "08.04.2026")]["time_msk"], "08.04.2026, 21:00")
        self.assertEqual(by_name_and_date[("FOMC Press Conference", "29.04.2026")]["time_msk"], "29.04.2026, 21:30")
        self.assertEqual(by_name_and_date[("FOMC Meeting", "29.04.2026")]["time_msk"], "29.04.2026, 21:00")

    def test_dead_source_does_not_block_valid_source_output(self) -> None:
        sources = [
            {
                "name": "broken_cpi",
                "reader": update_event_calendar._read_bls_schedule_events,
                "url": "https://example.test/broken-cpi",
                "event": "US CPI",
                "category": "macro",
                "impact": "high",
                "note": "BLS Consumer Price Index release.",
                "priority": 80,
            },
            {
                "name": "employment",
                "reader": update_event_calendar._read_bls_schedule_events,
                "url": "https://example.test/employment",
                "event": "US Employment Situation (NFP)",
                "category": "macro",
                "impact": "high",
                "note": "BLS Employment Situation release.",
                "priority": 80,
            },
        ]

        def fake_fetch_text(url: str, *, timeout: int) -> str:
            self.assertGreater(timeout, 0)
            if url.endswith("/broken-cpi"):
                raise OSError("404 not found")
            if url.endswith("/employment"):
                return BLS_EMPLOYMENT_HTML
            raise AssertionError(f"unexpected url: {url}")

        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            output = Path(td) / "event_calendar.json"
            payload, wrote, errors = update_event_calendar.refresh_event_calendar(
                output,
                now_utc=NOW_UTC,
                sources=sources,
                fetch_text_url=fake_fetch_text,
                horizon_days=7,
            )

            self.assertTrue(wrote)
            self.assertTrue(any("broken_cpi" in item for item in errors))
            self.assertEqual([item["event"] for item in payload["events"]], ["US Employment Situation (NFP)"])
            self.assertEqual(payload["events"][0]["time_msk"], "03.04.2026, 15:30")

    def test_refresh_writes_schema_valid_calendar_json_with_official_sample_data(self) -> None:
        sources = [
            {
                "name": "fed_fomc",
                "reader": update_event_calendar._read_fed_fomc_events,
                "url": "https://example.test/fed-fomc",
            },
            {
                "name": "fed_calendar",
                "reader": update_event_calendar._read_fed_calendar_events,
                "url": "https://example.test/fed-calendar",
            },
            {
                "name": "employment",
                "reader": update_event_calendar._read_bls_schedule_events,
                "url": "https://example.test/employment",
                "event": "US Employment Situation (NFP)",
                "category": "macro",
                "impact": "high",
                "note": "BLS Employment Situation release.",
                "priority": 80,
            },
        ]

        def fake_fetch_text(url: str, *, timeout: int) -> str:
            self.assertGreater(timeout, 0)
            mapping = {
                "https://example.test/fed-fomc": FED_FOMC_HTML,
                "https://example.test/fed-calendar": FED_CALENDAR_INDEX_HTML,
                "https://example.test/newsevents/2026-april.htm": FED_APRIL_HTML,
                "https://example.test/newsevents/2026-may.htm": FED_MAY_HTML,
                "https://example.test/employment": BLS_EMPLOYMENT_HTML,
            }
            if url not in mapping:
                raise AssertionError(f"unexpected url: {url}")
            return mapping[url]

        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            output = Path(td) / "event_calendar.json"
            payload, wrote, errors = update_event_calendar.refresh_event_calendar(
                output,
                now_utc=NOW_UTC,
                sources=sources,
                fetch_text_url=fake_fetch_text,
                horizon_days=7,
            )

            self.assertTrue(wrote)
            self.assertEqual(errors, [])
            self.assertTrue(payload["events"])
            raw = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("generated_at_utc", raw)
            self.assertIn("events", raw)
            self.assertTrue(raw["events"])
            for item in raw["events"]:
                self._assert_schema_valid_event(item)

            saved = read_event_calendar(output)
            self.assertEqual([item["event"] for item in saved["events"]], ["US Employment Situation (NFP)", "Jerome Powell speech"])

    def test_empty_calendar_case_writes_empty_events_list(self) -> None:
        sources = [
            {
                "name": "employment",
                "reader": update_event_calendar._read_bls_schedule_events,
                "url": "https://example.test/employment",
                "event": "US Employment Situation (NFP)",
                "category": "macro",
                "impact": "high",
                "note": "BLS Employment Situation release.",
                "priority": 80,
            }
        ]

        def fake_fetch_text(_url: str, *, timeout: int) -> str:
            self.assertGreater(timeout, 0)
            return "<html><body><table></table></body></html>"

        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            output = Path(td) / "event_calendar.json"
            payload, wrote, errors = update_event_calendar.refresh_event_calendar(
                output,
                now_utc=NOW_UTC,
                sources=sources,
                fetch_text_url=fake_fetch_text,
                horizon_days=7,
            )

            self.assertTrue(wrote)
            self.assertEqual(errors, [])
            self.assertEqual(payload.get("events"), [])
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved.get("events"), [])

    def test_total_source_failure_preserves_last_valid_file(self) -> None:
        with TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as td:
            output = Path(td) / "event_calendar.json"
            original = {
                "generated_at_utc": "2026-04-04T08:00:00Z",
                "events": [
                    {
                        "time_msk": "05.04.2026, 15:30",
                        "date_msk": "05.04.2026",
                        "event": "US CPI",
                        "category": "macro",
                        "impact": "high",
                        "window_before_min": 90,
                        "window_after_min": 120,
                        "source": "auto",
                        "note": "Macro release can reset volatility and risk appetite.",
                    }
                ],
            }
            output.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            sources = [
                {
                    "name": "employment",
                    "reader": update_event_calendar._read_bls_schedule_events,
                    "url": "https://example.test/employment",
                    "event": "US Employment Situation (NFP)",
                    "category": "macro",
                    "impact": "high",
                    "note": "BLS Employment Situation release.",
                    "priority": 80,
                }
            ]

            def fake_fetch_text(_url: str, *, timeout: int) -> str:
                self.assertGreater(timeout, 0)
                raise OSError("boom")

            payload, wrote, errors = update_event_calendar.refresh_event_calendar(
                output,
                now_utc=NOW_UTC,
                sources=sources,
                fetch_text_url=fake_fetch_text,
                horizon_days=7,
            )

            self.assertFalse(wrote)
            self.assertTrue(errors)
            self.assertEqual(payload.get("events")[0]["event"], "US CPI")
            self.assertEqual(read_event_calendar(output)["events"][0]["event"], "US CPI")


if __name__ == "__main__":
    unittest.main()
