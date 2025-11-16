import json
import time
from pathlib import Path
from typing import Any, Dict

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "journal.jsonl"


def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def journal_event(event: Dict[str, Any], kind: str = "raw_update") -> None:
    """
    Простейший журналинг:
    - пишет каждое событие в logs/journal.jsonl
    - формат: { ts, kind, event }
    """
    _ensure_log_dir()
    record = {
        "ts": time.time(),
        "kind": kind,
        "event": event,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
