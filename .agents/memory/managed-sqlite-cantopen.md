---
name: Managed SQLite CANTOPEN contention
description: Environment-specific behavior observed when the bot performs concurrent reads and bulk writes against its large WAL-backed SQLite file.
---

The managed bot's existing WAL-backed SQLite database can accept isolated read/write transactions while live concurrent scan activity reports `sqlite3.OperationalError: unable to open database file`. An absolute database path and valid directory/file/WAL permissions do not by themselves eliminate the failure; bounded retries and a small connection pool were also insufficient in the observed runtime.

**Why:** The failure occurs only during the live scan workload, while the same existing file accepts direct and SQLAlchemy insert probes. Claiming success from startup, unit tests, or an isolated probe is unsafe.

**How to apply:** Require a completed live cycle with fetched > 0, attempted > 0, persisted > 0, increased database state, and responsive health before considering a SQLite persistence change complete. Avoid deleting, rebuilding, redirecting, or maintenance-copying the existing database without explicit authorization.

The persistence boundary keeps CANTOPEN separate from writer locks, records the stage through SQLAlchemy lifecycle diagnostics, preserves the original traceback, and invalidates a failed session before any bounded fresh-session retry.

**Why:** Reusing a pooled session after a connection/open failure could turn a transient access error into repeated batch loss, while generic lock handling obscured the distinction.

**How to apply:** Keep CANTOPEN retries narrow, bounded, and limited to the confirmed bulk persistence path; never broaden them into a process-wide lock or silently mark failed batches successful.