---
name: Directional persistence policy
description: Active props are rescanned continuously, but only strong OVER/UNDER evaluations are stored as opportunity or candidate records.
---

Only directional OVER/UNDER evaluations may create persistent opportunity or candidate records. PASS, no-direction, and watchlist states are transient scan results; the underlying active prop remains in the canonical monitoring universe and is rescanned until a strong direction is available.

**Why:** Persistent PASS/watchlist rows made transient evidence gaps look like tracked props and allowed obsolete secondary classifications to influence reporting.

**How to apply:** Keep scan/evaluation logic active for every canonical prop, but filter persistence at the database/API boundary and before candidate batch inserts. Preserve the final Telegram delivery gate and delivery safety controls.