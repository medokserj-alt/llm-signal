"""Explicit Telegram channel capabilities for end-user routing."""
from __future__ import annotations

from dataclasses import dataclass
import os


def _int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip().lstrip("-").isdigit():
        return None
    return int(str(raw).strip())


@dataclass(frozen=True)
class ChannelProfile:
    profile_id: str
    chat_id: int | None
    telegram_user_ids: tuple[int, ...] = ()
    language: str = "ru"
    minimal_mode: bool = True
    receive_day: bool = True
    receive_mid: bool = True
    receive_scheduled_signals: bool = False
    receive_own_requested_signals: bool = True
    receive_urgent_news: bool = True
    receive_calendar_events: bool = True
    receive_positive_request_windows: bool = True
    receive_negative_windows: bool = False
    receive_technical_advisories: bool = False


def end_user_profiles() -> tuple[ChannelProfile, ...]:
    v_uid = _int_env("V_TELEGRAM_USER_ID")
    # Existing configured Dima identity; environment may restate it but must not remap it.
    dima_uid = _int_env("TG_DIMA_TELEGRAM_USER_ID") or _int_env("DIMA_TELEGRAM_USER_ID") or 8556231754
    return (
        ChannelProfile(
            profile_id="END_USER_V",
            chat_id=_int_env("V_CHAT_ID"),
            telegram_user_ids=(v_uid,) if v_uid is not None else (),
            receive_scheduled_signals=True,
        ),
        ChannelProfile(
            profile_id="END_USER_DIMA",
            chat_id=_int_env("TG_DIMA_CHAT_ID") or _int_env("DIMA_CHAT_ID"),
            telegram_user_ids=(dima_uid,) if dima_uid is not None else (),
            receive_scheduled_signals=False,
        ),
    )


def profile_for_user(user_id: int | None) -> ChannelProfile | None:
    if user_id is None:
        return None
    return next((p for p in end_user_profiles() if user_id in p.telegram_user_ids), None)


def profile_for_chat(chat_id: int | None) -> ChannelProfile | None:
    if chat_id is None:
        return None
    return next((p for p in end_user_profiles() if p.chat_id == chat_id), None)


def report_end_user_chat_ids(kind: str) -> list[int]:
    attr = "receive_day" if kind.lower() == "day" else "receive_mid"
    return [p.chat_id for p in end_user_profiles() if p.chat_id is not None and getattr(p, attr)]


def scheduled_end_user_chat_ids() -> list[int]:
    return [p.chat_id for p in end_user_profiles() if p.chat_id is not None and p.receive_scheduled_signals]
