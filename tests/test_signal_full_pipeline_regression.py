import json
import os
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNAL_PATH = REPO_ROOT / "signal"


class TestSignalFullPipelineRegression(unittest.TestCase):
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

                    if script == "send_last_signal_to_aia.py":
                        last_json = Path(_arg_value(args, "--last-json"))
                        data = json.loads(last_json.read_text(encoding="utf-8"))
                        data["published_at"] = "2026-04-12T12:00:00Z"
                        data["signal_json_v1"] = {
                            "signal_id": "sig-fresh-1",
                            "symbol": data["symbol"],
                            "direction": data["direction"],
                            "entry_zone": data["entry_range"],
                            "entry_price": data["entry_price_neutral"],
                            "sl": data["sl"],
                            "tp": {"tp1": data["tp1"], "tp2": data["tp2"]},
                            "published_at": data["published_at"],
                            "channel_id": -1001234567890,
                        }
                        last_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        print("send_signal_to_aia(full): OK")
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
            env.pop("SIGNAL_SKIP_AIA_SEND", None)

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
            self.assertEqual(out["signal_json_v1"]["symbol"], "NEW/USDT")
            self.assertEqual(out["signal_json_v1"]["entry_zone"], [10.0, 12.0])


if __name__ == "__main__":
    unittest.main()
