---
name: Market-only signals are non-actionable
description: Market movement can remain monitoring context, but cannot create a directional recommendation without legitimate player-result evidence.
---

## Rule

Missing or invalid player-result history must produce no directional recommendation,
even when the composite score and line-vs-average movement are strong. Preserve the
market signal as monitoring context for rescans and later enrichment; never
fabricate result windows, rates, or evidence from it.

The canonical delivery boundary independently requires both `OVER`/`UNDER` and
legitimate evidence. Evidence detectors may accept real history, explicit
supporting validation, or a populated evidence/intelligence trace, but ordinary
decision metadata is not evidence.

**Why:** A market-only bypass turned line movement into an actionable alert without
player-result support, violating the separation between monitoring and betting
recommendations.

**How to apply:** Keep evaluating, persisting observations, and rescanning
evidence-missing props. Gate all direct and queued delivery paths through the
shared direction-and-evidence check.