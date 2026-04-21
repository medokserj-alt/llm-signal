import json
import os
import re
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNAL_PATH = REPO_ROOT / "signal"


class TestSignalFullPipelineRegression(unittest.TestCase):
    def test_full_mode_creates_run_scoped_html_artifact_and_reports_it(self) -> None:
        with TemporaryDirectory(dir=REPO_ROOT) as td:
            temp_root = Path(td)
            logs_dir = temp_root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)

            fake_python = temp_root / "fake_python.py"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    from pathlib import Path


                    def _arg_value(args, flag):
                        try:
                            idx = args.index(flag)
                        except ValueError:
                            return None
                        if idx + 1 >= len(args):
                            return None
                        return args[idx + 1]


                    argv = sys.argv[1:]
                    script = Path(argv[0]).name if argv else ""
                    args = argv[1:]

                    if script == "get_signal_json.py":
                        payload = {
                            "time_msk": "16.04.2026, 15:21",
                            "symbol": "HTML/USDT",
                            "price": 42.0,
                            "direction": "long",
                            "entry_range": [41.0, 42.0],
                            "entry_price_neutral": 41.5,
                            "sl": 40.0,
                            "tp1": 43.0,
                            "tp2": 44.0,
                            "rr": 2.0,
                            "mode": "neutral",
                            "entry_mode": "pullback",
                            "sl_by_mode": {"neutral": 40.0},
                            "tp_by_mode": {"neutral": {"tvh1": 43.0, "tvh2": 44.0}},
                            "rr_by_mode": {"neutral": 2.0},
                            "exit_plan_by_mode": {"neutral": "plan"},
                            "entries": {"neutral": {"enabled": True}},
                            "multi_tf_view": {"m5": "—", "m15": "—", "h1": "—", "h4": "—", "d1": "—"},
                            "why_asset": "test",
                            "news_context": [],
                            "market_context": {"bias": "bullish", "summary": "flow context"},
                            "no_trade": False,
                        }
                        print(json.dumps(payload, ensure_ascii=False))
                        raise SystemExit(0)

                    if script == "save_analysis_text.py":
                        _ = sys.stdin.read()
                        print("analysis saved")
                        raise SystemExit(0)

                    if script == "postprocess_full_last.py":
                        raise SystemExit(0)

                    if script == "render_strict.py":
                        data = json.loads(sys.stdin.read())
                        output_path = _arg_value(args, "--output")
                        if not output_path:
                            raise SystemExit("render_strict.py missing --output")
                        Path(output_path).write_text(f"<html>{data['symbol']}</html>", encoding="utf-8")
                        print(f"<html>{data['symbol']}</html>")
                        raise SystemExit(0)

                    raise SystemExit(f"unexpected script: {script}")
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["SIGNAL_PYTHON_BIN"] = str(fake_python)
            env["SIGNAL_LOGS_DIR"] = str(logs_dir)
            env["SIGNAL_SKIP_AIA_SEND"] = "1"

            proc = subprocess.run(
                [str(SIGNAL_PATH), "full"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(proc.returncode, 0, msg=f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

            match = re.search(r"^✅ Signal HTML:\s+(.+)$", proc.stdout, re.MULTILINE)
            self.assertIsNotNone(match, msg=proc.stdout)
            rel_html_path = match.group(1).strip()
            self.assertRegex(rel_html_path, r"^signal_\d{8}_\d{6}\.html$")

            html_path = REPO_ROOT / rel_html_path
            try:
                self.assertTrue(html_path.exists(), msg=f"missing html artifact: {html_path}")
                self.assertEqual(html_path.read_text(encoding="utf-8"), "<html>HTML/USDT</html>")
            finally:
                if html_path.exists():
                    html_path.unlink()

    def test_full_mode_does_not_reuse_seeded_canonical_last_json(self) -> None:
        with TemporaryDirectory(dir=REPO_ROOT) as td:
            temp_root = Path(td)
            logs_dir = temp_root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)

            seeded_last = logs_dir / "last.json"
            seeded_last.write_text(
                json.dumps(
                    {
                        "symbol": "OLD/USDT",
                        "direction": "short",
                        "entry_range": [1.0, 2.0],
                        "debug_marker": "stale-seed",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            fake_python = temp_root / "fake_python.py"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    from pathlib import Path


                    def _arg_value(args, flag):
                        try:
                            idx = args.index(flag)
                        except ValueError:
                            return None
                        if idx + 1 >= len(args):
                            return None
                        return args[idx + 1]


                    argv = sys.argv[1:]
                    script = Path(argv[0]).name if argv else ""
                    args = argv[1:]

                    if script == "get_signal_json.py":
                        payload = {
                            "time_msk": "12.04.2026, 12:00",
                            "symbol": "NEW/USDT",
                            "price": 11.2,
                            "direction": "long",
                            "entry_range": [10.0, 12.0],
                            "entry_price_neutral": 11.0,
                            "sl": 9.5,
                            "tp1": 13.0,
                            "tp2": 14.0,
                            "rr": 2.0,
                            "mode": "neutral",
                            "entry_mode": "pullback",
                            "sl_by_mode": {"neutral": 9.5},
                            "tp_by_mode": {"neutral": {"tvh1": 13.0, "tvh2": 14.0}},
                            "entries": {"neutral": {"range": {"min": 10.0, "max": 12.0}}},
                            "no_trade": False,
                        }
                        print("=== [POOL OVERVIEW] ===")
                        print("fresh full candidate")
                        print(json.dumps(payload, ensure_ascii=False))
                        raise SystemExit(0)

                    if script == "save_analysis_text.py":
                        _ = sys.stdin.read()
                        print("analysis saved")
                        raise SystemExit(0)

                    if script == "postprocess_full_last.py":
                        last_json = Path(_arg_value(args, "--last-json"))
                        data = json.loads(last_json.read_text(encoding="utf-8"))
                        data["postprocessed"] = True
                        last_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        print("postprocess_full_last: OK")
                        raise SystemExit(0)

                    if script == "render_strict.py":
                        data = json.loads(sys.stdin.read())
                        print(f"<html>{data['symbol']}</html>")
                        raise SystemExit(0)

                    raise SystemExit(f"unexpected script: {script}")
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["SIGNAL_PYTHON_BIN"] = str(fake_python)
            env["SIGNAL_LOGS_DIR"] = str(logs_dir)
            proc = subprocess.run(
                [str(SIGNAL_PATH), "full"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(proc.returncode, 0, msg=f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

            out = json.loads(seeded_last.read_text(encoding="utf-8"))
            self.assertEqual(out["symbol"], "NEW/USDT")
            self.assertEqual(out["direction"], "long")
            self.assertNotIn("debug_marker", out)
            self.assertTrue(out["postprocessed"])
            self.assertNotIn("signal_json_v1", out)
            self.assertNotIn("published_at", out)

    def test_full_mode_ignores_stomped_shared_stream_tmp_and_uses_run_scoped_stream(self) -> None:
        with TemporaryDirectory(dir=REPO_ROOT) as td:
            temp_root = Path(td)
            logs_dir = temp_root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)

            fake_python = temp_root / "fake_python.py"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path


                    def _arg_value(args, flag):
                        try:
                            idx = args.index(flag)
                        except ValueError:
                            return None
                        if idx + 1 >= len(args):
                            return None
                        return args[idx + 1]


                    argv = sys.argv[1:]
                    script = Path(argv[0]).name if argv else ""
                    args = argv[1:]
                    logs_dir = Path(os.environ["SIGNAL_LOGS_DIR"])

                    if script == "get_signal_json.py":
                        fresh_payload = {
                            "time_msk": "15.04.2026, 12:00",
                            "symbol": "FRESH/USDT",
                            "price": 21.5,
                            "direction": "long",
                            "entry_range": [20.0, 22.0],
                            "entry_price_neutral": 21.0,
                            "sl": 19.4,
                            "tp1": 23.0,
                            "tp2": 24.5,
                            "rr": 2.0,
                            "mode": "neutral",
                            "entry_mode": "pullback",
                            "sl_by_mode": {"neutral": 19.4},
                            "tp_by_mode": {"neutral": {"tvh1": 23.0, "tvh2": 24.5}},
                            "entries": {"neutral": {"range": {"min": 20.0, "max": 22.0}}},
                            "no_trade": False,
                        }
                        stale_payload = {
                            "symbol": "STALE/USDT",
                            "direction": "short",
                            "entry_range": [1.0, 2.0],
                            "debug_marker": "shared-stream-stomped",
                        }
                        print(json.dumps(fresh_payload, ensure_ascii=False))
                        (logs_dir / "stream.tmp").write_text(
                            json.dumps(stale_payload, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        raise SystemExit(0)

                    if script == "save_analysis_text.py":
                        _ = sys.stdin.read()
                        print("analysis saved")
                        raise SystemExit(0)

                    if script == "postprocess_full_last.py":
                        last_json = Path(_arg_value(args, "--last-json"))
                        data = json.loads(last_json.read_text(encoding="utf-8"))
                        data["postprocessed"] = True
                        last_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        print("postprocess_full_last: OK")
                        raise SystemExit(0)

                    if script == "render_strict.py":
                        data = json.loads(sys.stdin.read())
                        print(f"<html>{data['symbol']}</html>")
                        raise SystemExit(0)

                    raise SystemExit(f"unexpected script: {script}")
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["SIGNAL_PYTHON_BIN"] = str(fake_python)
            env["SIGNAL_LOGS_DIR"] = str(logs_dir)
            proc = subprocess.run(
                [str(SIGNAL_PATH), "full"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(proc.returncode, 0, msg=f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

            out = json.loads((logs_dir / "last.json").read_text(encoding="utf-8"))
            self.assertEqual(out["symbol"], "FRESH/USDT")
            self.assertEqual(out["direction"], "long")
            self.assertNotIn("debug_marker", out)
            self.assertTrue(out["postprocessed"])

            shared_stream = (logs_dir / "stream.tmp").read_text(encoding="utf-8")
            self.assertIn("FRESH/USDT", shared_stream)
            self.assertNotIn("shared-stream-stomped", shared_stream)

    def test_full_mode_never_invokes_standalone_aia_send(self) -> None:
        with TemporaryDirectory(dir=REPO_ROOT) as td:
            temp_root = Path(td)
            logs_dir = temp_root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)

            fake_python = temp_root / "fake_python.py"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    from pathlib import Path


                    argv = sys.argv[1:]
                    script = Path(argv[0]).name if argv else ""

                    if script == "get_signal_json.py":
                        payload = {
                            "time_msk": "17.04.2026, 12:00",
                            "symbol": "NOAIA/USDT",
                            "price": 10.0,
                            "direction": "long",
                            "entry_range": [9.5, 10.0],
                            "entry_price_neutral": 9.8,
                            "sl": 9.0,
                            "tp1": 10.5,
                            "tp2": 11.0,
                            "rr": 2.0,
                            "mode": "neutral",
                            "entry_mode": "pullback",
                            "sl_by_mode": {"neutral": 9.0},
                            "tp_by_mode": {"neutral": {"tvh1": 10.5, "tvh2": 11.0}},
                            "entries": {"neutral": {"range": {"min": 9.5, "max": 10.0}}},
                            "no_trade": False,
                        }
                        print(json.dumps(payload, ensure_ascii=False))
                        raise SystemExit(0)

                    if script == "save_analysis_text.py":
                        _ = sys.stdin.read()
                        print("analysis saved")
                        raise SystemExit(0)

                    if script == "postprocess_full_last.py":
                        raise SystemExit(0)

                    if script == "render_strict.py":
                        _ = json.loads(sys.stdin.read())
                        print("<html>ok</html>")
                        raise SystemExit(0)

                    if script == "send_last_signal_to_aia.py":
                        raise SystemExit("standalone AIA send must not be invoked")

                    raise SystemExit(f"unexpected script: {script}")
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["SIGNAL_PYTHON_BIN"] = str(fake_python)
            env["SIGNAL_LOGS_DIR"] = str(logs_dir)

            proc = subprocess.run(
                [str(SIGNAL_PATH), "full"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(proc.returncode, 0, msg=f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
            self.assertNotIn("send_signal_to_aia(full)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
