---
name: Monitoring baseline August 2026
description: Observed production-like Underdog cycle timing and the measured bottleneck before optimization.
---

Database reads, especially the known-property-key and latest-snapshot pool lookups, dominated three post-restore Underdog cycles; provider latency and pure scoring CPU were comparatively small. OpenDota was too rarely exercised to establish a reliable provider miss/error baseline.

**Why:** The instrumentation-only window showed the combined scoring/evidence stage was mostly waiting on database reads, while provider calls contributed seconds rather than tens of minutes.

**How to apply:** Any optimization proposal should first target the measured database read path and separately add enough OpenDota-specific samples to distinguish provider misses from database wait time.