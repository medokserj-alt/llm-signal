#!/usr/bin/env bash
set -euo pipefail

LOG_PATH="${PERSONAL_BOT_LOG_PATH:-/root/llm-signal-next/personal_bot.log}"

ps aux | rg -n "tg_personal_bot.py"
tail -n 50 "$LOG_PATH"
