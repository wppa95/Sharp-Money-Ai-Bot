---
name: Direction-only prop delivery
description: Active prop delivery and strongest-pick selection are independent of tier, bet quality, market quality, and PASS labels.
---

The active delivery policy is a directional recommendation plus provenance-backed evidence. Telegram keeps eligible OVER/UNDER picks after the canonical evidence gate; a later selector must not reimpose all-six-window or confidence thresholds. Historical-average and completed-result evidence remain valid only when the provider/sample provenance gate passes.

**Why:** Score-class gates caused qualified evidence-backed props to be rejected or inconsistently handled between scan paths and final Telegram delivery. Historical-average provenance can live directly on a decision object, so both the collection gate and the Telegram formatter must inspect that field instead of requiring serialized validation JSON.

**How to apply:** New delivery paths should call the canonical directional/evidence gate once at collection and again as a final backstop. Preserve live/past, freshness, identity, deduplication, and Telegram transport checks; do not add a second strength filter after qualification.