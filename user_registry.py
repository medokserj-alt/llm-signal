#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
REGISTRY_PATH = Path(
    os.getenv("TELEGRAM_USER_REGISTRY_PATH", BASE / "user_registry.json")
)
DEFAULT_TRIAL_COUNT = int(os.getenv("TELEGRAM_TRIAL_COUNT", "5") or "5")

VALID_STATUSES = {"registered", "trial", "paid", "blocked"}


def _utc_now_z() -> str:
    return datetime.utcnow().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"users": {}}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"users": {}}
    if not isinstance(data, dict):
        return {"users": {}}
    users = data.get("users")
    if not isinstance(users, dict):
        data["users"] = {}
    return data


def _save_registry(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(REGISTRY_PATH.parent),
            prefix=f"{REGISTRY_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, REGISTRY_PATH)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _normalize_user(user) -> dict[str, Any]:
    return {
        "uid": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def get_user(uid: int) -> dict | None:
    data = _load_registry()
    users = data.get("users", {})
    return users.get(str(uid))


def get_all_users() -> dict[str, dict]:
    data = _load_registry()
    users = data.get("users")
    return users if isinstance(users, dict) else {}


def register_user(user) -> tuple[dict, bool]:
    data = _load_registry()
    users = data.get("users", {})
    uid = str(user.id)
    now = _utc_now_z()
    created = False
    record = users.get(uid)
    if not isinstance(record, dict):
        record = {
            "uid": user.id,
            "status": "registered",
            "trial_remaining": 0,
            "signal_bot_started": False,
            "created_at": now,
            "updated_at": now,
        }
        created = True
    record.update(_normalize_user(user))
    record["updated_at"] = now
    users[uid] = record
    data["users"] = users
    _save_registry(data)
    return record, created


def set_status(uid: int, status: str) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown status: {status}")
    data = _load_registry()
    users = data.get("users", {})
    uid_str = str(uid)
    record = users.get(uid_str) if isinstance(users.get(uid_str), dict) else {"uid": uid}
    record["status"] = status
    record.setdefault("trial_remaining", 0)
    record.setdefault("signal_bot_started", False)
    record["updated_at"] = _utc_now_z()
    users[uid_str] = record
    data["users"] = users
    _save_registry(data)
    return record


def start_trial(user, count: int | None = None) -> dict:
    if count is None:
        count = DEFAULT_TRIAL_COUNT
    record, _ = register_user(user)
    record["status"] = "trial"
    record["trial_remaining"] = int(count)
    record["updated_at"] = _utc_now_z()
    data = _load_registry()
    users = data.get("users", {})
    users[str(user.id)] = record
    data["users"] = users
    _save_registry(data)
    return record


def set_paid(user) -> dict:
    record, _ = register_user(user)
    record["status"] = "paid"
    record["trial_remaining"] = int(record.get("trial_remaining") or 0)
    record["updated_at"] = _utc_now_z()
    data = _load_registry()
    users = data.get("users", {})
    users[str(user.id)] = record
    data["users"] = users
    _save_registry(data)
    return record


def set_signal_bot_started(uid: int, started: bool = True) -> dict:
    data = _load_registry()
    users = data.get("users", {})
    uid_str = str(uid)
    record = users.get(uid_str) if isinstance(users.get(uid_str), dict) else {"uid": uid}
    record.setdefault("status", "registered")
    record.setdefault("trial_remaining", 0)
    record["signal_bot_started"] = bool(started)
    record["updated_at"] = _utc_now_z()
    users[uid_str] = record
    data["users"] = users
    _save_registry(data)
    return record


def is_active(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    if status == "paid":
        return True
    if status == "trial":
        try:
            return int(record.get("trial_remaining") or 0) > 0
        except Exception:
            return False
    return False


def consume_trial(uid: int) -> dict | None:
    data = _load_registry()
    users = data.get("users", {})
    uid_str = str(uid)
    record = users.get(uid_str)
    if not isinstance(record, dict):
        return None
    if record.get("status") != "trial":
        return record
    try:
        remaining = int(record.get("trial_remaining") or 0)
    except Exception:
        remaining = 0
    if remaining <= 0:
        record["trial_remaining"] = 0
        record["status"] = "registered"
    else:
        remaining -= 1
        record["trial_remaining"] = remaining
        if remaining <= 0:
            record["status"] = "registered"
    record["updated_at"] = _utc_now_z()
    users[uid_str] = record
    data["users"] = users
    _save_registry(data)
    return record


def status_text(record: dict | None) -> str:
    if not isinstance(record, dict):
        return "Статус: не зарегистрирован"
    status = record.get("status") or "registered"
    trial_remaining = record.get("trial_remaining")
    started = record.get("signal_bot_started")
    parts = [f"Статус: {status}"]
    if status == "trial":
        parts.append(f"Осталось trial-сигналов: {trial_remaining}")
    parts.append(f"Signal bot активирован: {'да' if started else 'нет'}")
    return "\n".join(parts)
