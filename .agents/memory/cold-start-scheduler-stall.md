---
name: Cold-start scheduler stall
description: Runtime observation about Underdog cold-start state restoration and overlapping background jobs
---

Cold-start Underdog monitoring can remain active for many minutes before provider fetch begins, with the wait occurring during database state restoration while player-history and FPR jobs are active in the same process.

**Why:** A live restart showed no provider-fetch or cycle-completion boundary for more than eight minutes, while player-history provider calls and a full-pool rescan continued and later Underdog triggers were skipped.

**How to apply:** Treat cold-start restore as a separate measured stage. Do not infer scoring or provider-enrichment bottlenecks from a cycle that has not cleared state restoration, and do not change scheduler behavior until completed-cycle data exists.