---
name: Restart session isolation
description: Runtime monitoring cursors and child tasks must reset on every bot restart while SQLite evidence and delivery history remain durable.
---

Every bot restart starts a new disposable monitoring session. Health-sidecar
cursors, scan checkpoints, rotation/grader offsets, live provider telemetry,
and process-local scan/task state must be cleared or canceled before jobs are
registered. SQLite observations, completed player-game evidence, grading,
learning, and persistent PLAY delivery records remain intact; delivery records
may still be read to prevent duplicate alerts.

**Why:** Resuming interrupted monitoring work made a restarted process look
like the old session and could replay or skip work. A fresh session is
deterministic while the database still provides the historical context needed
for reports, grading, and deduplication.

**How to apply:** Treat health JSON as live-session telemetry only. Reset
persisted offsets at startup, clear/cancel in-memory provider and FPR tasks,
then let the first normal scan restore only durable delivery/history state.