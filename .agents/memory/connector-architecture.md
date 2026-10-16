---
name: Provider and connector architecture
description: How active pick'em providers remain isolated from historical sportsbook data
---

Underdog uses its public pick'em projections endpoint; PrizePicks uses the
PropLine feed. Both are equal primary platforms. PropLine also supplies
shared supplemental evidence for either platform through the canonical
pipeline, preserving each pick's platform and original line.

**Why:** The user explicitly replaced the Underdog-only primary-source policy.

**How to apply:** Follow [primary platform scope](provider-scope-separation.md)
for evidence reuse and individual-pick-only delivery.

Pick'em isolation is enforced by `MarketSnapshot.is_pickem=True` on Underdog
output. Consensus and sportsbook-only analysis must never turn pick'em rows
into sportsbook prices or a second alert universe. Historical sportsbook rows
may remain readable for compatibility, but no retired sportsbook provider may
resume network ingestion without an explicit user request.