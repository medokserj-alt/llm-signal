#!/usr/bin/env bash
set -euo pipefail

services=(llm-signal-bot-dev.service llm-signal-bot-clone.service)

echo "Stopping: ${services[*]}"
systemctl stop "${services[@]}"

echo "Disabling: ${services[*]}"
systemctl disable "${services[@]}"

echo "Status:"
systemctl --no-pager --full status "${services[@]}" || true
