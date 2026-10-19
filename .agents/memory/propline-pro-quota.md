---
name: PropLine Pro quota boundary
description: Subscription capability and shared quota rules for every PropLine client process.
---

All authenticated PropLine traffic shares a cross-process, restart-safe operating target of 960 requests per UTC day, a hard maximum of 1,000, and 40 calls per rolling hour (90-second spacing). Normal operation leaves 40 requests in reserve. This applies to ingestion, history, results, analytics, and both primary platforms together. While configured for Free, keep permitted feed/discovery/raw game and event-stat endpoints active, and reject paid-only history, settlement, and analytics before reserving quota. Paid endpoint code remains available when the configured plan is Hobby or higher. Provider daily-quota headers are telemetry, not permission to raise these limits. Provider Retry-After cooldowns apply across processes.

**Why:** The user explicitly required approximately 950–960 calls/day with a 1,000/day hard maximum, superseding the earlier subscription-sized operating policy, and currently requires Free-plan-safe collection without paid history or settlement calls. The bot and API facade share one credential; advertised plan allowances do not override the user's spending limits.

**How to apply:** Route every permitted authenticated request through the shared persistent ledger; plan-restricted endpoints must fail before reservation or network I/O. Keep paid endpoint code behind the explicit plan setting so an upgrade can restore it without a code change. Keep permitted ingestion active, back off complete zero-result snapshots as already configured, and do not repeatedly poll unavailable paid families. Never relax the 1,000/day hard cap or global pacing.