#!/usr/bin/env bash
set -euo pipefail

services=(llm-signal-bot-dev.service llm-signal-bot-clone.service)

echo "Enabling: ${services[*]}"
systemctl enable "${services[@]}"

echo "Starting: ${services[*]}"
systemctl start "${services[@]}"

echo "Status:"
systemctl --no-pager --full status "${services[@]}" || true
