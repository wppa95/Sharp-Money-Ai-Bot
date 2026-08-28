---
name: Command report resilience
description: How Telegram status/report commands should behave during SQLite contention.
---

Reporting commands must return partial health data when a database counter is
busy or unavailable; one slow or locked counter must not turn the whole command
into a failure response.

**Why:** The monitoring database is intentionally busy with long-running scans
and concurrent writes, so aggregate reporting can occasionally hit SQLite
timeouts even while the bot and providers remain healthy.

**How to apply:** Use short independent timeouts for optional counters, render
unavailable values honestly, and preserve provider/scheduler health sections.
Keep the live monitoring path independent from report-only database reads.