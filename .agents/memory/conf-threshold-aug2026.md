---
name: Confidence threshold values (Aug 2026)
description: Current authoritative confidence gate values after the Aug 2026 raise.
---

## Current Values

| Config key | Old | New |
|---|---|---|
| `UD_MIN_CONF_S` | 80 | 85 |
| `UD_MIN_CONF_A` | 70 | 75 |
| `UD_NON_STRICT_MIN_CONF_A` | 70 | 75 |

## How to Apply

Any test asserting on these values must use 85 / 75. Tests that call `_passes_gate("S", 80)` or `_passes_gate("A", 70)` are stale — update to 85 / 75.

**Why:** Thresholds were raised to require stronger evidence before S/A-tier alerts fire. All three are set in `config.py` defaults.
