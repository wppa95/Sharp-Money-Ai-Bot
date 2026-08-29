---
name: Provider observation ordering
description: Durable ordering and identity rules for normalized player-prop monitoring.
---

Raw provider observations must be persisted before scoring, enrichment, or Telegram delivery. Scoring should read the prior history for the same provider-owned source identity, and metadata refreshes must update the current row without inserting a second snapshot.

**Why:** A scoring or delivery failure must not make the provider appear unobserved, and alternate lines/source IDs for the same player/stat must not contaminate one another's movement history.

**How to apply:** Use append-only normalized history for monitoring ingestion, source/event-aware lookups for scoring and lifecycle updates, and derive tracked/reporting populations from the latest current row per provider source identity rather than candidate or delivery logs.