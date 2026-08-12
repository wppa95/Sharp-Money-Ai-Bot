---
name: Stable-refresh cursor_end bug
description: set_stable_refresh_stats stored next_cursor (=0 after pool wrap) as "cursor_end", causing /funnel to always show 0.0% progress.
---

# Stable Refresh cursor_end bug

## The rule
Always store `end_cursor` (the actual batch end position) as `"cursor_end"` in `set_stable_refresh_stats()`, not `next_cursor` (which wraps to 0 when the full pool is covered in one batch).

**Why:** When the batch size (10k) exceeds the pool size (~9k), `end_cursor = pool_size` and `next_cursor = pool_size % pool_size = 0`. Storing `next_cursor` makes `/funnel` compute `0 / pool_size = 0.0%` progress — misleading the user into thinking nothing happened.

**How to apply:** In `market_engine._stable_refresh_job`, the stats dict must use `"cursor_end": end_cursor` not `"cursor_end": next_cursor`. The console log correctly used `end_cursor` for its own `_progress` calculation; only the persisted stats had the bug.

## Related display change
Removed raw "Cursor: N → N" from stable refresh console log and /funnel output. Replaced with human-readable "Progress: X%" + "Coverage: N / M active props".

## FPR note
FPR stats correctly store `fpr_end_cursor` (not `fpr_next_cursor`) — no bug there.

## FPR first-run slowness
The FPR job's first run can be slow (>5 min) when it competes with the initial underdog_job scan for the SQLite DB. APScheduler then skips the second trigger ("maximum number of running instances reached"). This resolves naturally after the first underdog scan completes — subsequent FPR runs complete quickly.
