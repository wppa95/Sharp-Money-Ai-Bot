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

When the shared monitoring completion marker is delayed by a deferred SQLite
drain, status may use the latest completed Underdog scan timestamp as a
display-only freshness fallback; this must not be treated as shared-cycle
completion for scheduler accounting.

**Why:** The feed scan can finish and record its results before background
bookkeeping drains, so showing only the shared barrier makes a healthy monitor
look stale.

**How to apply:** Prefer the shared-cycle timestamp, but fall back to the
current-session scan timestamp for user-facing freshness only.

For `/funnel`, the per-cycle `ScanCycleLog` aggregate is a valid reporting
fallback when the larger normalized-history population query times out.

**Why:** SQLite can be busy draining the current monitoring cycle while the
cycle log is already available; treating the optional population query as
mandatory incorrectly reported the entire funnel as unavailable.

**How to apply:** Render scan-cycle counts when available, mark only the
normalized population section as degraded if needed, and show UNAVAILABLE only
when both the cycle log and population sources fail.