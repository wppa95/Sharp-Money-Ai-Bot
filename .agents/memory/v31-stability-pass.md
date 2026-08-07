---
name: V3.1 memory / stability pass
description: Targeted fixes for OOM kills, persistent state recovery, and accurate RSS measurement.
---

# V3.1 Stability Pass — Memory & Persistent State

## Fixes applied

### Memory growth (OOM kills)
- `_MARKET_FIRST_ALERT` dict was unbounded — entries added on every alert, only deleted on prop removal.
  - Fix: TTL eviction at start of each `underdog_job` cycle (24h via `_MARKET_FIRST_ALERT_TTL_H`).
- `get_known_underdog_prop_keys()` previously returned ALL distinct (player, stat) pairs ever stored.
  - Fix: Added `since_days=60` cutoff — only props seen in last 60 days returned, limiting peak set size.
- RSS measurement was `ru_maxrss` (all-time high-water mark, never decreases — misleading).
  - Fix: Added `_rss_mb()` that reads VmRSS from /proc/self/status for actual current RSS.
- Added `gc.collect()` after `_scored_props.clear()` to help Python return freed pages to allocator pool.

### Persistent state recovery
- Added `_init_state_from_db(db)` called on every cold start (first cycle per process).
  - Restores `_MARKET_FIRST_ALERT` from recently alerted props in DB (within 24h window).
  - Logged as: "State recovery: restored N market first-alert entries from DB".
- Added `db.get_first_alert_times_ud(since_hours=24)` — new DB method for startup hydration.
- Existing alert dedup is already DB-backed (line-change comparison via prev_record, has_recent_ud_alert, flip cooldown) — no regression on restart.

## Observed memory pattern (live, post-fix)
- Scan 1 (cold start, ~5,580 props): 90.7 → 163.2 MB (+72.5 MB)
- Scan 2 (incremental, 5 changed props): 163.2 → 198.2 MB (+34.9 MB)
- Delta decreasing per scan — allocator pool fills, stabilizes around 225-250 MB.
- Previous misleading pattern: `ru_maxrss` appeared to grow 50 MB per scan regardless.

## Why the OOM kills were happening
All unexpected_exit entries: shutdown_at=null, session_secs=null — pure SIGKILL (atexit never ran).
`_MARKET_FIRST_ALERT` was accumulating entries without eviction.
`get_known_underdog_prop_keys()` was loading increasingly large sets as DB grew.

**How to apply:** Monitor VmRSS log lines. If VmRSS keeps growing past 400 MB after scan 3+,
investigate `recent_by_key` (all active props per cycle) as the next candidate.
