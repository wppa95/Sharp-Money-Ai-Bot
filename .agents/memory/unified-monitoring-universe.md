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

Every distinct provider threshold and named line variant must enter the neutral
grading ledger before scoring, even when it is not actionable or delivered.

**Why:** Provider IDs can be reused across moved thresholds and across
standard/goblin/demon lines; collapsing them destroys line-specific hit, miss,
push, and historical-rate evidence.

**How to apply:** Include threshold and provider variant in grading identity,
append raw line history, and treat complete tracking persistence as a hard
barrier before evaluation.

FPR must never apply delivery deduplication before scoring. Recently delivered
props still need history, evaluation, and tracking work; dedup belongs only at
the alert-delivery gate.

ScanCycleLog.active is a cumulative per-scan observation total, not the current
active-universe size. Funnel coverage denominators must come from the FPR
canonical active by-line pool, and FPR rescored counts must be included in the
period scoring total.

**Why:** Summing active rows across repeated scans made the funnel compare a
cumulative observation count with one current FPR batch/pool and falsely imply
that unchanged props were never rescored.

**How to apply:** Label scan totals as observations, display FPR coverage as
covered/current canonical pool, and aggregate scoring across live, stable
refresh, and FPR paths.