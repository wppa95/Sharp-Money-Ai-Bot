---
name: Monitoring baseline August 2026
description: Observed production-like Underdog cycle timing and the measured bottleneck before optimization.
---

Database reads, especially the known-property-key and latest-snapshot pool lookups, dominated three post-restore Underdog cycles; provider latency and pure scoring CPU were comparatively small. OpenDota was too rarely exercised to establish a reliable provider miss/error baseline.

**Why:** The instrumentation-only window showed the combined scoring/evidence stage was mostly waiting on database reads, while provider calls contributed seconds rather than tens of minutes.

**How to apply:** Any optimization proposal should first target the measured database read path and separately add enough OpenDota-specific samples to distinguish provider misses from database wait time.

The controlled zero-target cold-start test confirmed that disabling persisted-pool scoring alone does not guarantee a short first cycle: the first post-restart scan remained active for more than 20 minutes without a completion summary, while `max_instances=1` overlap skips continued normally.

**Why:** The scan still performs the pre-loop pool/key reads and continues normal new-prop and line-movement handling, so the experiment isolates scoring but does not isolate all startup work.

**How to apply:** Treat the live result as incomplete/negative rather than claiming Railway-like cadence; do not optimize further without a separately approved experiment targeting the remaining measured startup stages.