---
name: Monitoring cycle budget and funnel resilience
description: Optional player-history enrichment must never hold the shared monitoring cycle or funnel report open.
---

The complete Underdog feed is persisted before evaluation. Live scans bulk-load existing player evidence once and do not launch per-prop provider enrichment; an explicitly empty bulk result is authoritative for that scan. The independent history collector owns missing-evidence collection and bulk result writes.

**Why:** Per-prop reads/provider calls made a large live scan look stalled even when persistence itself was batched; repeated enrichment attempts also prevented cycle completion and left `/funnel` without fresh scan evidence.

**How to apply:** Use one bulk evidence read for the live feed, bulk-upsert fetched results in the history collector, keep live scans read-only with respect to enrichment, and retain a lightweight scan-cycle report fallback. Treat `None` as “no bulk preload supplied”; do not treat `[]` as permission to reopen SQLite or call a provider per prop.

PropLine live-feed rotation must be bounded per cycle and resume from its cursor; only football is excluded, while MLB and every other catalog sport remain eligible.

**Why:** Serially polling every sport across four bookmakers held one background ingestion pass open for roughly 10–12 minutes. After that was bounded, synchronous current-state plus raw-snapshot persistence of a 12,000-row Underdog feed still kept the scan slot open past two minutes, especially when the full-book history collector overlapped.

**How to apply:** Budget optional provider rotation below the live cadence, then measure cycle completion after the post-scan durability barrier—not at the earlier evaluation-summary checkpoint. Keep the history collector from competing with cadence-critical full-feed persistence.