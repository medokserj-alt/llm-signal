# Scheduled Automation

The scheduler is implemented in `scheduled_runner.py` and is invoked by thin scripts in `scripts/`.
All due logic is calculated in MSK (`Europe/Moscow`) and every decision row stores both
`slot_time_msk` and `generated_at_utc`.

## Environment

```bash
SCHEDULED_START_DATE_MSK=2026-06-05
SCHEDULED_PUBLISH_CHAT_IDS=-1003492385200,-1003493070625,-1003530482991

SCHEDULED_DAY_ENABLED=1
SCHEDULED_DAY_TIME_MSK=09:00

SCHEDULED_MID_ENABLED=1
SCHEDULED_MID_TIME_MSK=08:45
SCHEDULED_MID_INTERVAL_DAYS=3

SCHEDULED_SIGNAL_ENABLED=1
SCHEDULED_SIGNAL_DEFAULT_MODE=aggressive
SCHEDULED_SIGNAL_SLOTS_MSK=09:30,12:30,15:30,18:30,21:30,00:30
SCHEDULED_SIGNAL_RETRY_DELAY_MINUTES=60
SCHEDULED_SIGNAL_MAX_ATTEMPTS=2
SCHEDULED_SIGNAL_AIA_GATE_MODE=soft

SCHEDULED_SIGNAL_STATE_PATH=logs/scheduled_signal_state.json
SCHEDULED_PUBLISH_STATE_PATH=logs/scheduled_publish_state.json
```

Optional AIA context path overrides:

```bash
AIA_MARKET_WINDOW_PATH=/root/llm-signal-ai-agent/logs/market_window_advisory_latest.json
AIA_EVENT_RISK_PATH=/root/llm-signal-ai-agent/logs/event_risk_context_latest.json
AIA_FLOW_CONTEXT_PATH=/root/llm-signal-ai-agent/logs/flow_derivatives_context_v2.json
```

## Timers

Timers run every five minutes and the Python runner checks exact MSK due windows.
`Persistent=false` avoids systemd backfilling missed timer firings.

```bash
sudo cp systemd/ai-agent-scheduled-*.service systemd/ai-agent-scheduled-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-agent-scheduled-day.timer ai-agent-scheduled-mid.timer ai-agent-scheduled-signal.timer
systemctl list-timers 'ai-agent-scheduled-*'
```
