---
name: PropLine pagination and snapshot safety
description: Durable rules for extending the authorized PropLine client without publishing partial feeds or overspending its daily quota.
---

PropLine pagination must run until provider exhaustion, stopping only on normal completion or a repeated request/page signature; arbitrary discovery ceilings hide data and are not a safe loop guard.

**Why:** Sports and event catalogs can grow beyond a fixed page count, while a repeated cursor can otherwise create an unbounded request loop.

**How to apply:** Count every uncached PropLine HTTP request through the shared daily budget, keep event-scoped fetching opt-in when scheduler behavior must remain unchanged, and retain the last complete snapshot whenever discovery or an event fetch is incomplete.

Event IDs are not always durable: after a scoped odds 404, rediscovery may retry once only when home team, away team, and start time identify the same event.

**Why:** Provider event identifiers can rotate between catalog and odds calls, but retrying an unmatched event risks mixing games.

**How to apply:** Use stable provider IDs when present and normalized event/player/market fallbacks otherwise; never guess a replacement ID from an unrelated event.