#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import tg_bot as _tg_bot_mod
from tg_bot import (
    ALLOWED_UIDS,
    FALLBACK_CHANNEL,
    PAID_ALLOWED_UIDS,
    _AIA_TEST_CHANNEL_ID_INT,
    _build_signal_json_v1,
    _infer_signal_id,
    _read_last_signal_json,
    _should_send_to_aia_for_target,
    _utc_now_z,
    get_all_users,
    get_main_publication_chat_id,
    is_active,
    send_signal_to_aia,
)

LAST_JSON_PATH = Path(__file__).resolve().parent / "logs" / "last.json"


def _normalize_chat_id_list(raw) -> list[int]:
    helper = getattr(_tg_bot_mod, "_normalize_chat_id_list", None)
    if callable(helper):
        return helper(raw)
    if isinstance(raw, str):
        items = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    out: list[int] = []
    seen: set[int] = set()
    for item in items:
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, int):
            value = int(item)
        else:
            s = str(item).strip()
            if not s or not s.lstrip("-").isdigit():
                continue
            value = int(s)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _get_main_fanout_targets(primary_chat_id: int | None) -> list[int]:
    helper = getattr(_tg_bot_mod, "_get_main_fanout_targets", None)
    if callable(helper):
        return helper(primary_chat_id)
    return []


def _channel_sort_key(record: dict) -> int:
    if not isinstance(record, dict):
        return 0
    raw = record.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int(dt.astimezone(timezone.utc).timestamp())
    except Exception:
        return 0


def _allowed_for_aia(uid: int) -> bool:
    if ALLOWED_UIDS or PAID_ALLOWED_UIDS:
        return uid in ALLOWED_UIDS or uid in PAID_ALLOWED_UIDS
    return True


def _read_last_payload(last_json_path: Path) -> dict | None:
    try:
        data = json.loads(last_json_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_last_payload(last_json_path: Path, *, allow_tg_bot_fallback: bool) -> dict | None:
    if allow_tg_bot_fallback:
        try:
            data = _read_last_signal_json()
        except Exception:
            data = None
        if isinstance(data, dict) and data:
            return data
    data = _read_last_payload(last_json_path)
    return data if isinstance(data, dict) else None


def _iter_candidate_channel_ids(last_payload: dict | None):
    raw = os.getenv("SIGNAL_AIA_CHANNEL_ID")
    if raw is not None and str(raw).strip():
        yield raw

    if isinstance(last_payload, dict):
        last_channel = last_payload.get("channel_id")
        if last_channel is not None:
            yield last_channel

    if _AIA_TEST_CHANNEL_ID_INT is not None:
        yield _AIA_TEST_CHANNEL_ID_INT

    if FALLBACK_CHANNEL is not None and str(FALLBACK_CHANNEL).strip():
        yield FALLBACK_CHANNEL

    users = get_all_users()
    ranked: list[tuple[int, int]] = []
    if isinstance(users, dict):
        for uid_str, record in users.items():
            if not isinstance(uid_str, str) or not uid_str.lstrip("-").isdigit():
                continue
            if not isinstance(record, dict) or not is_active(record):
                continue
            uid = int(uid_str)
            if not _allowed_for_aia(uid):
                continue
            ranked.append((_channel_sort_key(record), uid))

    for _, uid in sorted(ranked, reverse=True):
        channel_id = get_main_publication_chat_id(uid)
        if channel_id is not None:
            yield channel_id


def _resolve_channel_id(last_payload: dict | None = None):
    seen: set[int] = set()
    for raw in _iter_candidate_channel_ids(last_payload):
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, int):
            value = int(raw)
        else:
            s = str(raw).strip()
            if not s or not s.lstrip("-").isdigit():
                continue
            try:
                value = int(s)
            except Exception:
                continue
        if value in seen:
            continue
        seen.add(value)
        return value
    return None


def _resolve_publish_targets(channel_id: int | None, last_payload: dict | None) -> list[int]:
    existing = _normalize_chat_id_list(last_payload.get("publish_targets")) if isinstance(last_payload, dict) else []
    if existing:
        return existing
    if channel_id is None:
        return []
    return [channel_id]


def _persist_final_signal_payload(signal_json_v1: dict, *, last_json_path: Path | None = None) -> None:
    if not isinstance(signal_json_v1, dict):
        return
    if last_json_path is None:
        last_json_path = LAST_JSON_PATH
    data = _read_last_payload(last_json_path)
    if not isinstance(data, dict):
        return

    data["signal_json_v1"] = signal_json_v1

    published_at = signal_json_v1.get("published_at")
    if published_at is not None:
        data["published_at"] = published_at

    entry_price = signal_json_v1.get("entry_price")
    if entry_price is not None:
        data["entry_price"] = entry_price

    sl = signal_json_v1.get("sl")
    if sl is not None:
        data["sl"] = sl

    tp = signal_json_v1.get("tp")
    if isinstance(tp, dict):
        if tp.get("tp1") is not None:
            data["tp1"] = tp.get("tp1")
        if tp.get("tp2") is not None:
            data["tp2"] = tp.get("tp2")
        if tp.get("tp3") is not None:
            data["tp3"] = tp.get("tp3")

    signal_id = signal_json_v1.get("signal_id")
    if signal_id:
        data["signal_id"] = signal_id

    channel_id = signal_json_v1.get("channel_id")
    if channel_id is not None:
        data["channel_id"] = channel_id

    last_json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_no_trade_last_json(data: dict | None) -> bool:
    return isinstance(data, dict) and bool(data.get("no_trade"))


def _parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("run_log", nargs="?")
    parser.add_argument("--last-json", dest="last_json", default=None)
    args, _ = parser.parse_known_args(argv)
    return args


def main() -> int:
    if os.getenv("DRY_RUN") == "1":
        print("send_signal_to_aia(full): SKIP (DRY_RUN)")
        return 0

    args = _parse_args(sys.argv[1:])
    run_log = Path(args.run_log) if args.run_log and args.run_log.strip() else None
    explicit_last_json = bool(args.last_json)
    last_json_path = Path(args.last_json).resolve() if explicit_last_json else LAST_JSON_PATH
    last_payload = _load_last_payload(last_json_path, allow_tg_bot_fallback=not explicit_last_json)
    published_at = _utc_now_z()
    signal_id = _infer_signal_id(None, run_log, published_at)
    try:
        channel_id = _resolve_channel_id(last_payload)
    except TypeError:
        channel_id = _resolve_channel_id()
    publish_targets = _resolve_publish_targets(channel_id, last_payload)

    if _is_no_trade_last_json(last_payload):
        print("send_signal_to_aia(full): SKIP (NO_TRADE)")
        return 0

    signal_json_v1 = _build_signal_json_v1(
        signal_id=signal_id,
        published_at=published_at,
        channel_id=channel_id,
        origin_chat_id=channel_id,
        publish_targets=publish_targets,
        symbol_hint=None,
        last_payload=last_payload,
        last_json_path=last_json_path,
    )
    if not signal_json_v1:
        print("send_signal_to_aia(full): FAIL (payload_build)")
        return 0

    _persist_final_signal_payload(signal_json_v1, last_json_path=last_json_path)

    if channel_id is None:
        print("send_signal_to_aia(full): FAIL (channel_id_missing)")
        return 0

    if not _should_send_to_aia_for_target(channel_id):
        print(f"send_signal_to_aia(full): SKIP (target_gated channel_id={channel_id})")
        return 0

    ok = send_signal_to_aia(signal_json_v1)
    print(f"send_signal_to_aia(full): {'OK' if ok else 'FAIL (http_send)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
