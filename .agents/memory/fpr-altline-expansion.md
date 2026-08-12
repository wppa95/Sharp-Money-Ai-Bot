---
name: FPR full-pool alt-line expansion
description: Why FPR uses get_all_active_underdog_snapshots_by_line while stable-refresh keeps get_active_underdog_snapshot_per_prop.
---

# FPR full-pool alt-line expansion

## The rule
The full-pool rescan job (`_full_pool_rescan_job`) must call
`db.get_all_active_underdog_snapshots_by_line()`, which groups by
`(player_name, stat_type, line_value)` and returns every alt-line variant as a
separate pool entry.

The stable-refresh job and watchlist-rescan continue to call
`db.get_active_underdog_snapshot_per_prop()`, which groups by
`(player_name, stat_type)` only and returns one (the latest) row per prop.

**Why:** Underdog exposes ~10-11 alt-line variants per player+stat (e.g. Points at
24.5, 25.5, 26.5, …).  The per-prop method collapses these to ~9,756 unique
(player, stat) pairs; the by-line method returns all ~104k unique
(player, stat, line) triplets.  The /funnel "active" count is a cumulative scan
sum (≈ cycles × ~5,500 props/cycle), which matches the ~104k figure the user sees
when comparing against the FPR pool.

**How to apply:** FPR pool dict keys are 3-tuples `(player_name, stat_type, line_value)`.
Sort key and scoring loop unpack 3-tuple: `(player, stat, line), snap = item`.
`_fpr_line_val` from the key is redundant (line already on snap record) but required
for correct tuple unpacking.  Dedup set is still keyed by `(player, stat)` 2-tuple.

## DB method signature
```python
async def get_all_active_underdog_snapshots_by_line(self) -> dict[tuple[str, str, float], UnderdogSnapshotRecord]
```
Groups MAX(id) by (player_name, stat_type, line_value), filters removed=False.
Location: database.py, immediately after get_active_underdog_snapshot_per_prop.
