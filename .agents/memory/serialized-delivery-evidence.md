---
name: Serialized delivery evidence
description: Provenance and sample-count requirements for persisted evidence used by delivery gates
---

Persisted evidence is eligible for directional delivery only when it contains both an allowed result source and a usable sample count. A source label alone, including `propline`, is not sufficient. Every scan path (new, line-change, stable-refresh, and full-pool) must pass its typed validation object into the shared evidence helper; passing only score/decision metadata silently blocks otherwise valid picks.

**Why:** Serialized payloads can outlive the typed validation object that originally produced them, so trusting provenance without its count can bypass the five-game safety boundary after restart or during report reconciliation.

**How to apply:** Keep typed validation checks and serialized checks aligned at every delivery call site; `propline` requires at least five supplemental samples, composite sources require five total real/supplemental samples, and primary result sources require five real samples.