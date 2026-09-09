---
name: Monitoring cycle budget and funnel resilience
description: Optional player-history enrichment must never hold the shared monitoring cycle or funnel report open.
---

The complete Underdog feed is persisted before evaluation. Live scans bulk-load existing player evidence once and do not launch per-prop provider enrichment; the independent history collector owns missing-evidence collection and bulk result writes.

**Why:** Per-prop reads/provider calls made a large live scan look stalled even when persistence itself was batched; repeated enrichment attempts also prevented cycle completion and left `/funnel` without fresh scan evidence.

**How to apply:** Use one bulk evidence read for the live feed, bulk-upsert fetched results in the history collector, keep live scans read-only with respect to enrichment, and retain a lightweight scan-cycle report fallback.