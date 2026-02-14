#!/usr/bin/env bash
set -euo pipefail
cd /root/llm-signal-next

if [[ -f /root/llm-signal-next/.env.tg.clean ]]; then
  set -a
  set +u
  source /root/llm-signal-next/.env.tg.clean
  set -u
  set +a
fi

if [[ -z "${TELEGRAM_PERSONAL_BOT_TOKEN:-}" ]]; then
  echo "Set TELEGRAM_PERSONAL_BOT_TOKEN in env"
  exit 1
fi

LOG_PATH="${PERSONAL_BOT_LOG_PATH:-/root/llm-signal-next/personal_bot.log}"
SUBS_PATH="${TELEGRAM_SUBSCRIPTIONS_PATH:-/root/llm-signal-next/user_subscriptions.personal.json}"
SUBS_LOG_PATH="${TELEGRAM_SUBSCRIPTIONS_LOG_PATH:-/root/llm-signal-next/user_subscriptions.personal.log.jsonl}"
STATE_PATH="${TELEGRAM_PERSONAL_SIGNAL_STATE_PATH:-/root/llm-signal-next/personal_signal_state.json}"
POLL_SEC="${TELEGRAM_PERSONAL_POLL_SEC:-10}"
SIGNAL_ROOT="${TELEGRAM_SIGNAL_ROOT:-/root/llm-signal-next}"
CLEANUP_DAYS="${TELEGRAM_SIGNAL_CLEANUP_DAYS:-30}"
CLEANUP_SEC="${TELEGRAM_SIGNAL_CLEANUP_SEC:-21600}"

nohup env \
  TELEGRAM_PERSONAL_BOT_TOKEN="$TELEGRAM_PERSONAL_BOT_TOKEN" \
  TELEGRAM_SUBSCRIPTIONS_PATH="$SUBS_PATH" \
  TELEGRAM_SUBSCRIPTIONS_LOG_PATH="$SUBS_LOG_PATH" \
  TELEGRAM_PERSONAL_SIGNAL_STATE_PATH="$STATE_PATH" \
  TELEGRAM_PERSONAL_POLL_SEC="$POLL_SEC" \
  TELEGRAM_SIGNAL_ROOT="$SIGNAL_ROOT" \
  TELEGRAM_SIGNAL_CLEANUP_DAYS="$CLEANUP_DAYS" \
  TELEGRAM_SIGNAL_CLEANUP_SEC="$CLEANUP_SEC" \
  /root/llm-signal-next/.venv/bin/python3 tg_personal_bot.py \
  > "$LOG_PATH" 2>&1 &
