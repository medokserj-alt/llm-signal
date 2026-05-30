# Mode Contract V2

This document fixes the intended user-facing meaning of the three public modes without changing live trading math in this batch.

## Contract

### Aggressive

- Role: active tactical trading mode.
- Default horizon: `intraday / 1–2 days`.
- Primary objective: capture `1–2 TP`.
- Continuation is allowed only if structure remains strong and post-entry context remains supportive.
- This mode is not "maximum signals at any cost".

### Neutral

- Role: safer aggressive / swing-tactical mode.
- Default horizon: `1–3 days / short swing`.
- Entry and confirmation should be cleaner than aggressive.
- In future batches the risk envelope may widen slightly, but this batch does not change thresholds.

### Conservative

- Role: strategic / position-style mode.
- Default horizon: `2–5 days / multi-day`.
- This is a distinct holding / management style, not just a deeper aggressive or neutral entry.

## Current Batch Scope

- Align prompt, UI, and wording with this contract.
- Preserve current live trading logic.
- Do not change:
  - direction logic
  - SL / TP math
  - confirm thresholds
  - wait-confirm deadlines

## Code Surface

- Prompt contract: `prompt_system.txt`
- User-facing mode wording: `tg_bot.py`, `tg_personal_bot.py`
- Horizon derivation: `get_signal_json.py`
- Rendered horizon labels: `render_strict.py`
