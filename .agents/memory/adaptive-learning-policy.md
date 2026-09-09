---
name: Adaptive learning policy
description: Guardrails for the provider-aware adaptive ranking weights.
---

Adaptive learning is an audited, provider × sport × market ranking adjustment only. It uses HIT rows plus MISS rows classified as Model, requires minimum sample safeguards, stays within a bounded multiplier/point adjustment, and is disabled by default. Rebuilds should remain explicit and descriptive unless the feature flag is enabled.

**Why:** Adaptive learning must not turn neutral sportsbook observations, variance, settlement issues, or small samples into directional alerts or change the existing qualification and delivery behavior.

**How to apply:** Keep active-weight reads optional and neutral when unavailable. Expose version, safeguards, eligible-group coverage, and the ranking-only boundary in reports whenever adaptive status is shown.