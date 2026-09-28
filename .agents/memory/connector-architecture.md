---
name: Provider and connector architecture
description: How active pick'em providers remain isolated from historical sportsbook data
---

Underdog uses its public pick'em projections endpoint and is the active
user-facing live source. PropLine supplies supplemental observations and
official evidence through the shared canonical pipeline.

Pick'em isolation is enforced by `MarketSnapshot.is_pickem=True` on Underdog
output. Consensus and sportsbook-only analysis must never turn pick'em rows
into sportsbook prices or a second alert universe. Historical sportsbook rows
may remain readable for compatibility, but no retired sportsbook provider may
resume network ingestion without an explicit user request.