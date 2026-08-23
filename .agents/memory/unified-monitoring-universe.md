---
name: Unified monitoring universe
description: V3.6 monitoring and rotation use one canonical active prop universe.
---

The background monitor treats every active prop line as one continuously
scannable universe. Delivery eligibility is separate from tracking and must not
create Watchlist, Near-Miss, Rejected, Qualified, Pass, or tier-based rotation
queues.

**Why:** Separate player/stat and by-line rotations caused overlapping work,
incomplete alternate-line coverage, and reporting populations that disagreed
with the actual feed.

**How to apply:** Keep the live feed responsible for new/moved/removed data,
use the canonical by-line active pool for bounded rescans, and report tracked
coverage, movement, rescoring, and delivery independently from alert gates.

FPR must never apply delivery deduplication before scoring. Recently delivered
props still need history, evaluation, and tracking work; dedup belongs only at
the alert-delivery gate.