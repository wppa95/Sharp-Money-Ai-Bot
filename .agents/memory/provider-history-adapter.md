---
name: Provider history adapter contract
description: Contract for adapting provider history rows into the shared prop scorer
---

When a provider-specific pipeline adapts persisted history into the shared scoring model, it must preserve every lifecycle field consumed by the scorer, including removal state.

**Why:** A PrizePicks cycle with existing history crashed at scoring time when its adapter omitted `removed`, even though the source rows contained it.

**How to apply:** Keep adapter objects structurally compatible with the shared Underdog scorer; when adding scorer fields, audit every provider adapter and add a regression test with persisted history.