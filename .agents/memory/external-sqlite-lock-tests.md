---
name: External SQLite lock tests
description: How to deterministically exercise Python-level retry behavior against a raw SQLite writer.
---

File-backed retry tests must lower the application connection's SQLite
`busy_timeout` before a raw `sqlite3` connection starts `BEGIN IMMEDIATE`.
Otherwise the normal production timeout absorbs the external lock internally,
so the Python retry loop is never exercised.

**Why:** The process-wide async writer gate only serializes this process's
`Database` sessions; a raw external writer bypasses it. A short busy timeout
exposes that boundary while keeping the test fast and deterministic.

**How to apply:** Hold the external transaction in a separate thread, release
it from the retry observation for recovery tests, and keep it held through all
attempts for exhaustion tests. Verify durable row counts after each outcome.