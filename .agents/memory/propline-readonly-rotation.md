---
name: PropLine read-only rotation
description: Durable rotation rules for supplemental PropLine endpoint coverage.
---

The supplemental PropLine endpoint families use a bounded scheduler lane with
stable, discovery-backed target identities and an atomic SQLite cursor. The
lane must share the existing persistent request limiter and leave explicit
capacity for the full player-history collector; a restart may repeat the last
request but must not skip a family or target.

**Why:** PropLine exposes useful read-only evidence across several endpoint
families, but independently scheduling every family can exhaust the shared
rolling-hour quota or create duplicate coverage after a restart.

**How to apply:** Build targets only from discovered official sports, active
player/stat inputs, and official event IDs. Keep supplemental responses out of
alert scoring. Only explicit won/lost/push/void outcomes with complete,
non-terminal evidence may become settlements; report all other outcomes as
pending with provenance.