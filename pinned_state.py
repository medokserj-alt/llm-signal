from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


PinKind = str  # "day" | "mid"


def _normalize_channel_id(channel_id: int | str) -> str:
    if isinstance(channel_id, int):
        return str(channel_id)
    s = str(channel_id).strip()
    if not s:
        raise ValueError("channel_id is empty")
    if not s.lstrip("-").isdigit():
        raise ValueError(f"channel_id is not numeric: {channel_id!r}")
    return str(int(s))


def load_pinned_state(path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, dict[str, int]] = {}
    for ch, v in raw.items():
        if not isinstance(ch, str):
            continue
        if not isinstance(v, dict):
            continue
        row: dict[str, int] = {}
        for kind in ("day", "mid"):
            mid = v.get(kind)
            if isinstance(mid, int):
                row[kind] = mid
        if row:
            out[ch] = row
    return out


def save_pinned_state(path: Path, state: dict[str, dict[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def get_pinned_message_id(
    state: dict[str, dict[str, int]],
    channel_id: int | str,
    kind: PinKind,
) -> int | None:
    if kind not in ("day", "mid"):
        raise ValueError(f"unknown kind: {kind!r}")
    ch = _normalize_channel_id(channel_id)
    row = state.get(ch) or {}
    mid = row.get(kind)
    return mid if isinstance(mid, int) else None


def set_pinned_message_id(
    state: dict[str, dict[str, int]],
    channel_id: int | str,
    kind: PinKind,
    message_id: int,
) -> dict[str, dict[str, int]]:
    if kind not in ("day", "mid"):
        raise ValueError(f"unknown kind: {kind!r}")
    if not isinstance(message_id, int):
        raise ValueError("message_id must be int")
    ch = _normalize_channel_id(channel_id)
    out: dict[str, dict[str, int]] = {}
    for k, v in state.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if kk in ("day", "mid") and isinstance(vv, int)}
    row = dict(out.get(ch) or {})
    row[kind] = message_id
    out[ch] = row
    return out

