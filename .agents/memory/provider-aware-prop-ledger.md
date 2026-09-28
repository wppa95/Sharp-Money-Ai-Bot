---
name: Provider-aware prop ledger
description: Durable boundary for combining player-prop observations without conflating evidence or delivery.
---

All supported player-prop feeds should enter one canonical observation/history/tracking lifecycle while retaining provider identity in every source and report population. Sportsbook observations can support evidence and settlement tracking, but must not silently become Underdog alert candidates.

**Why:** A blended row loses the ability to explain which provider supplied a line, which source settled it, and whether a report describes monitoring or actionable picks. It also risks turning confirmation data into an unverified delivery source.

**How to apply:** Normalize provider rows once, persist immutable observations plus current history and neutral tracking, preserve provider-aware identities through grading and rollups, and keep provider delivery gates separate from the shared ledger.

PropLine is the configured supplemental observation feed for PrizePicks and
Underdog rows. The native Underdog connector remains active and primary.
Historical sportsbook identities may remain in the ledger but are not live
inputs and must not create new rows.

When a provider is disabled, do not construct its client, register its usage
budget, or schedule its polling/status jobs; compatibility callbacks may remain
only for direct callers and must not be part of the live scheduler.

**Why:** A callback-level no-op still makes health, scheduler, and startup
reports claim the disabled provider is active and leaves accidental future call
sites harder to detect.

**How to apply:** Gate provider initialization, result clients, usage tracking,
and scheduler registration from the same provider-enabled flag.