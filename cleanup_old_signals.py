#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable


def cleanup_old_signals(
    root: Path,
    *,
    days: int = 30,
    patterns: Iterable[str] | None = None,
) -> dict:
    """
    Delete signal files older than `days` from `root`.
    Default patterns:
      - signal_*.html
      - logs/signal_*.log
    """
    root = Path(root)
    cutoff = time.time() - max(days, 0) * 24 * 3600
    patterns = patterns or ("signal_*.html", "logs/signal_*.log")

    deleted = 0
    scanned = 0
    for pattern in patterns:
        for path in root.glob(pattern):
            scanned += 1
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
            except Exception:
                continue
            try:
                path.unlink()
                deleted += 1
            except Exception:
                continue

    return {"deleted": deleted, "scanned": scanned, "days": days}


def _main() -> int:
    root = Path(os.getenv("TELEGRAM_SIGNAL_ROOT", Path.cwd()))
    days_raw = os.getenv("TELEGRAM_SIGNAL_CLEANUP_DAYS", "30")
    try:
        days = int(days_raw)
    except Exception:
        days = 30
    result = cleanup_old_signals(root, days=days)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
