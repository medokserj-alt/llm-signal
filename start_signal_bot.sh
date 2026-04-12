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

if [[ -z "${TELEGRAM_SIGNAL_BOT_TOKEN:-}" ]]; then
  echo "Set TELEGRAM_SIGNAL_BOT_TOKEN in env"
  exit 1
fi

LOG_PATH="${SIGNAL_BOT_LOG_PATH:-/root/llm-signal-next/signal_bot.log}"

nohup env \
  TELEGRAM_SIGNAL_BOT_TOKEN="$TELEGRAM_SIGNAL_BOT_TOKEN" \
  TELEGRAM_CORE_BOT_USERNAME="${TELEGRAM_CORE_BOT_USERNAME:-V3_bot}" \
  TELEGRAM_SIGNAL_BOT_USERNAME="${TELEGRAM_SIGNAL_BOT_USERNAME:-LLM_signals_pa_dev_bot}" \
  TELEGRAM_USER_REGISTRY_PATH="${TELEGRAM_USER_REGISTRY_PATH:-/root/llm-signal-next/user_registry.json}" \
  TELEGRAM_PA_ALLOWED_USER_IDS="${TELEGRAM_PA_ALLOWED_USER_IDS:-}" \
  /root/llm-signal-next/.venv/bin/python3 tg_signal_bot.py \
  > "$LOG_PATH" 2>&1 &
