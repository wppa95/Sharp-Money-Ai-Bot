---
name: Scan performance — bulk snapshot save
description: Root cause and fix for underdog_job scans exceeding the 120s poll interval.
---

**Root cause:** `db.save_underdog_snapshot(record)` was called inside the main loop for every prop (~4 400–5 400 per scan). Each call opened a new aiosqlite session, added one record, committed, refreshed, and closed. At ~30 ms overhead per call × 5 000 calls = ~150 s per scan. Scans routinely ran 73–229 s, causing APScheduler to skip every other 120 s fire.

**Fix applied (market_engine.py):**
- Added `_incremental_records: list = []` alongside `_cold_start_records`
- In the normal (non-cold-start) branch, `_incremental_records.append(record)` replaces the per-prop `await db.save_underdog_snapshot(record)` call
- After the main loop, a single `await db.save_underdog_snapshots_bulk(list(_incremental_records))` saves all records in one SQLite transaction
- The copy (`list(...)`) is essential — without it, `.clear()` on the original list empties the object the mock holds, corrupting `call_args` in tests

**Why `list(_incremental_records)` not `_incremental_records`:**
Mock stores a reference to the list object. `_incremental_records.clear()` mutates the same object. Tests then see an empty list via `call_args[0][0]`. Passing `list(...)` creates a fresh object unaffected by the clear.

**Result:**
- Before: avg 143 s, P95 ~225 s, 67% skip rate at 120 s interval
- After: ~65 s observed, 0 skips in first 2 post-cold-start cycles

**Tests updated:**
- `bot/tests/test_underdog_new_prop.py` — 10 assertions changed from `save_underdog_snapshot` → `save_underdog_snapshots_bulk` with `call_args[0][0][0]` to reach the first record in the list
- `bot/tests/test_scan_performance.py` — new 14-item regression file
