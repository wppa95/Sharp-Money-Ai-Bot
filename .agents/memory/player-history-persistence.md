---
name: Player history persistence boundary
description: Durable handling of collected player results after the provider fetch cadence expires
---

Completed player-history results must not be discarded merely because the provider
fetch cadence has expired. Continue draining bounded database write batches under
a separate persistence timeout, and report any rows that could not be written.

**Why:** A large active book can finish collection at the cadence boundary; using
the same deadline for writes turned a successful fetch into zero persisted
evidence and made later grading impossible.

**How to apply:** Treat fetch timing and evidence persistence as separate phases.
Keep SQLite history writes bulk-oriented with large bounded upsert batches
(currently 1,000 rows) so full-book collection maximizes data consumption; claim
the shared write window per batch, not for the entire drain, so the two-minute
FPR can run between history batches. Use only a small cooperative yield between
commits, and make supplemental provider calls timeout-bounded without replacing
valid primary-provider rows.