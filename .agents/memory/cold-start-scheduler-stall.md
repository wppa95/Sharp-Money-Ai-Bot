---
name: Cold-start scheduler stall
description: Runtime observation about Underdog cold-start state restoration and overlapping background jobs
---

Cold-start Underdog monitoring must not depend on full-table SQLite history reads. Recovery is best-effort and bounded, while the first fresh feed seeds compact in-process comparison state; later cycles use that state and persist snapshots normally.

**Why:** A live restart showed no provider-fetch or cycle-completion boundary for more than eight minutes, while player-history provider calls and a full-pool rescan continued and later Underdog triggers were skipped. After bounding recovery and replacing repeated grouped-history reads with compact prior-feed state, a fresh 8k-prop cycle completed and the next cycle completed in under a minute.

**How to apply:** Keep recovery and pre-loop history work non-blocking/bounded. Treat cold-start restore, provider fetch, scoring, and persistence as separate measured stages; verify at least one follow-up cycle before diagnosing scheduler behavior.