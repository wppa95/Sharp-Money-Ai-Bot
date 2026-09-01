---
name: OddsAPI strict pacing
description: Shared restart-safe request admission for the V4 daily cap and cooldown.
---

All real OddsAPI cache misses, including player-prop fetches, must pass through
one shared persisted admission gate. The gate reserves a request before HTTP,
enforces the local-day count and minimum interval atomically, and persists
before the request starts.

**Why:** A scheduler-only gate can be bypassed by another OddsAPI caller, while
in-memory counters reset on restart and can exceed the provider's plan limit.

**How to apply:** Keep the daily limit, interval, timezone, and state path in
central configuration. Treat cache hits as free, skipped requests as uncounted,
and failed requests as consumed reservations because the provider was contacted.