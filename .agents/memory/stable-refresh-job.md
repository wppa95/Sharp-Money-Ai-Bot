---
name: Stable refresh + watchlist job
description: Durable lessons from the _stable_refresh_job implementation — dedup semantics, watchlist cursor, and DB-persist requirements.
---

## Key design rules

**Bulk dedup success tracking**: Use a separate `_sr_bulk_dedup_ok: bool` flag — do NOT treat an empty `frozenset()` return as a load failure. An empty result means "nothing alerted recently" and is valid. Per-prop `has_recent_ud_alert` fallback fires ONLY when `get_recently_alerted_prop_keys()` raises.

**Why**: `elif not _sr_db_alerted` treats empty frozenset as False → triggers 10k serial SQLite sessions on the common "no recent alerts" case, defeating the entire bulk optimisation.

**How to apply**: Pattern is always `_ok=False / try: ...; _ok=True / except: pass`, then `if _ok: set-lookup else: per-prop-query`.

---

**95+ priority DB persist**: After a stable 95+ priority broadcast, call `mark_ud_snapshot_alert_sent` + `mark_opportunity_alert_sent`. In-memory sets (`_priority_override_sent`, `_prop_market_alerted`) are cleared on restart; without the DB write, the same prop fires again on the next stable-refresh cycle after reboot.

---

**Watchlist rotating cursor**: `get_active_watchlist_candidates()` returns rows ordered by `id ASC` (FIFO). The job reads `get_wl_refresh_cursor()` from health.json, slices `_wl_all[cursor:cursor+200]`, then writes `set_wl_refresh_cursor(end % pool)`. Without rotation, `[:200]` always processes the same first 200 candidates and starves the rest.

---

**Async test pattern**: Both SQLite integration classes (`TestGetActiveUnderdogSnapshotPerPropSQLite`, `TestStableRefreshJobE2E`) previously used manual `cls._loop = asyncio.new_event_loop()`. Converting to `@pytest.mark.asyncio` eliminated all aiosqlite event-loop-closed thread warnings.

**Why**: When a class-level loop closes after `teardown_class`, aiosqlite worker threads still try to call `call_soon_threadsafe` on the closed loop → `RuntimeError: Event loop is closed` in thread warning. pytest-asyncio manages the loop lifecycle cleanly.
