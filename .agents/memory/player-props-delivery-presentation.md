---
name: Player Props delivery presentation
description: Durable rules for the unified Player Props Telegram presentation and evidence-dependent strong labels.
---

The user-facing product is one Player Props feed with an explicit source label
for Underdog or PrizePicks/PropLine. Do not expose internal tiers, routing
metadata, or duplicate provider availability panels.

**Why:** Separate provider-oriented displays made alerts noisy and made a
market-direction recommendation look stronger than its completed-game evidence
justified.

**How to apply:** Render the current/previous line, movement, source, evidence
source/sample, direction-specific history, cleaned reason, status, and time.
Only use a strong-pick label when the validation provenance is explicit,
sample size is sufficient, and the selected OVER/UNDER has direction-specific
hit-rate support.

Provider-specific fetching and pacing remain separate, but production
evaluation and delivery must enter through one shared monitoring cycle. Run
provider ingestion concurrently with the current-book branch and bound its
wait; slow PropLine/PrizePicks requests must not stall Underdog processing or
create a second evaluation lane.

**Why:** A one-shot PropLine scheduler could stop after its initial request,
while synchronous ingestion could make the shared cycle coalesce later
Underdog ticks. Both failure modes split or starve the canonical Player Props
universe.

**How to apply:** Keep the shared cycle as the only production owner of the
fetch-to-evaluation handoff. Queue newly persisted provider observations for
the next deterministic evaluation snapshot, and cancel/log a provider fetch
that exceeds the bounded wait rather than blocking the whole cycle.