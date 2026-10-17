---
name: Provider and connector architecture
description: How active pick'em providers remain isolated from historical sportsbook data
---

Underdog uses its public pick'em projections endpoint; PrizePicks uses the
PropLine feed. PrizePicks is primary; Underdog is secondary. PropLine also supplies
shared supplemental evidence for either platform through the canonical
pipeline, preserving each pick's platform and original line.

**Why:** The user's latest comprehensive V4 audit/repair instructions replace
the earlier equal-primary policy while preserving both active feeds.

**How to apply:** Follow [primary platform scope](provider-scope-separation.md)
for evidence reuse and individual-pick-only delivery.

Pick'em isolation is enforced by `MarketSnapshot.is_pickem=True` on Underdog
output. Consensus and sportsbook-only analysis must never turn pick'em rows
into sportsbook prices or a second alert universe. Historical sportsbook rows
may remain readable for compatibility, but no retired sportsbook provider may
resume network ingestion without an explicit user request.