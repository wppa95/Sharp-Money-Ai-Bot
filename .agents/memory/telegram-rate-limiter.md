---
name: Telegram rate limiter
description: Global sliding-window flood protection for Telegram delivery — architecture and defaults.
---

## Architecture

New module: `engine/telegram_rate_limiter.py` — singleton `TelegramRateLimiter`.

### Layer 1 — per-window budget (configurable)
- `TG_RATE_WINDOW_SECONDS=300` (5-min rolling window)
- `TG_RATE_MAX_PER_WINDOW=5` (max 5 actionable alerts per window)
- When only 1 slot left, it is reserved for S/A-tier (B/C deferred unless meaningful change)
- S/A-tier meaningful changes (new prop OR line move ≥ 2× MIN_UNDERDOG_LINE_CHANGE) get one budget bypass

### Layer 2 — emergency flood protection
- `TG_FLOOD_THRESHOLD=10` — if this many alerts sent in window, engage flood mode
- `TG_FLOOD_PROTECTION_DURATION=600` — pause delivery for 10 min; bot continues scanning
- Nothing bypasses flood protection (not even meaningful changes)

### Bypass rules
- `removed=True` alerts bypass entirely (cleanup, not spam)
- `market_move_only=True` alerts bypass (internal, no Telegram)

### Hook points
- `alerts.py :: deliver_underdog()` — rate check AFTER daily cap, BEFORE format/broadcast (step 3a)
- `record_sent()` called synchronously after successful `broadcast_alert()`
- `reset_cycle_counters()` called at start of ud_snaps loop in `underdog_job`
- `log_cycle_summary()` called at end of `underdog_job` (before `_ud_full_scan_running = False`)

### DeliveryResult change
- Added `rate_limited: bool = False` field
- `__str__` shows "rate-limited(reason)" when True

### Test isolation
- Singleton bleeds state between tests — fixed via `conftest.py` autouse fixture that calls `reset_limiter()` before every test
- `reset_limiter()` module-level function discards the singleton (testing only)

**Why:** Bot was sending ~50 Telegram alerts in 5 minutes after a large batch of props simultaneously passed all per-prop gates. No global throttle existed. Grading thresholds deliberately not changed.

**How to apply:**
- Tune via env vars: TG_RATE_MAX_PER_WINDOW, TG_RATE_WINDOW_SECONDS, TG_FLOOD_THRESHOLD, TG_FLOOD_PROTECTION_DURATION
- When flood mode fires, logs show: `🚨 TelegramRateLimiter: FLOOD PROTECTION ENGAGED`
- End-of-cycle summary (WARNING level) shows: Qualified / Delivered / Deferred counts
