#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

KEY = "TELEGRAM_PA_ALLOWED_USER_IDS"
BASE = Path(__file__).resolve().parent
ENV_PATH = Path(os.getenv("TELEGRAM_ENV_PATH", BASE / ".env.tg.clean"))


def _parse_ids(raw: str) -> list[int]:
    out: list[int] = []
    for x in raw.replace(";", ",").split(","):
        x = x.strip()
        if not x:
            continue
        if x.lstrip("-").isdigit():
            try:
                out.append(int(x))
            except Exception:
                continue
    return list(dict.fromkeys(out))


def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def get_paid_allowed_ids() -> set[int]:
    raw = os.getenv(KEY, "")
    if ENV_PATH.exists():
        for line in _read_env_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(KEY + "="):
                raw = stripped.split("=", 1)[1]
                break
    return set(_parse_ids(raw))


def _write_env_lines(lines: list[str]) -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_env_value(value: str) -> None:
    lines = _read_env_lines()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(KEY + "="):
            prefix, *rest = line.split("#", 1)
            comment = "#" + rest[0] if rest else ""
            new_line = f"{KEY}={value}"
            if comment:
                new_line = new_line + " " + comment.strip()
            lines[i] = new_line
            updated = True
            break
    if not updated:
        lines.append(f"{KEY}={value}")
    _write_env_lines(lines)
    os.environ[KEY] = value


def add_paid_allowed_uid(uid: int) -> bool:
    ids = get_paid_allowed_ids()
    if uid in ids:
        return False
    ids.add(uid)
    new_value = ",".join(str(x) for x in sorted(ids))
    _set_env_value(new_value)
    return True


def remove_paid_allowed_uid(uid: int) -> bool:
    ids = get_paid_allowed_ids()
    if uid not in ids:
        return False
    ids.remove(uid)
    new_value = ",".join(str(x) for x in sorted(ids))
    _set_env_value(new_value)
    return True
