---
name: Serialized delivery evidence
description: Provenance and sample-count requirements for persisted evidence used by delivery gates
---

Persisted evidence is eligible for directional delivery only when it contains both an allowed result source and a usable sample count. A source label alone, including `propline`, is not sufficient.

**Why:** Serialized payloads can outlive the typed validation object that originally produced them, so trusting provenance without its count can bypass the five-game safety boundary after restart or during report reconciliation.

**How to apply:** Keep typed validation checks and serialized checks aligned; `propline` requires at least five supplemental samples, composite sources require five total real/supplemental samples, and primary result sources require five real samples.