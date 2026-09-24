---
name: Independent sport delivery quotas
description: Telegram delivery capacity is allocated independently per eligible sport.
---

There is no rolling per-sport alert-count quota. Every candidate that passes the existing direction, evidence, timing, freshness, and deduplication checks remains eligible for delivery.

MLB, NBA, NFL, and NHL remain lower delivery priority; other eligible sports are ordered first. This is ranking only and must not suppress candidates or cap sends.

**Why:** The previous two-pick rolling window could suppress otherwise eligible candidates after successful sends. The user requested its removal after seeing directional candidates in the funnel without corresponding alerts.

**How to apply:** Preserve all per-candidate eligibility and duplicate-alert safeguards. Do not add a rolling per-sport or global count cap unless the user requests one.