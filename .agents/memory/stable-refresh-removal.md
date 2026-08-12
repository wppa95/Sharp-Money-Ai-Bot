---
name: Stable refresh removal semantics
description: Why stable refresh uses get_active_underdog_snapshot_per_prop, not get_latest_underdog_snapshot_per_prop
---

# Stable refresh — correct snapshot pool method

**Rule:** `_stable_refresh_job` must call `db.get_active_underdog_snapshot_per_prop()`, never `db.get_latest_underdog_snapshot_per_prop()`.

**Why:** The old `get_latest_underdog_snapshot_per_prop()` computes `MAX(id) WHERE removed=False` per prop. If the actual latest feed record for a prop is a removal (removed=True), the old method skips it and returns an older non-removed snapshot — making the prop appear still active. The stable refresh would then rescore and potentially alert on a dead line.

`get_active_underdog_snapshot_per_prop()` computes `MAX(id)` over ALL rows first, then filters `WHERE removed=False`. So props whose latest record is a removal are simply absent from the result.

**How to apply:** Any job that needs the "currently active Underdog prop pool" for rescoring purposes should use `get_active_underdog_snapshot_per_prop()`. The old method remains valid for the underdog_job standing scan (which already handles removals via `[REMOVED]` marker in `selection` field) but should not be used as the stable-refresh pool source.

**Tests to verify:** `TestGetActiveUnderdogSnapshotPerProp` and `TestStableRefreshRemovalSemantics` in `bot/tests/test_stable_refresh.py`.
