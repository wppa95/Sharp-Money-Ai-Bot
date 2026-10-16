---
name: PropLine Pro quota boundary
description: Subscription capability and shared quota rules for every PropLine client process.
---

All authenticated PropLine traffic shares a cross-process, restart-safe operating target of 960 requests per UTC day, a hard maximum of 1,000, and 40 calls per rolling hour (90-second spacing). Normal operation leaves 40 requests in reserve. This applies to ingestion, history, results, analytics, and both primary platforms together. Provider daily-quota headers are telemetry, not permission to raise these limits. Provider Retry-After cooldowns apply across processes.

**Why:** The user explicitly required approximately 950–960 calls/day with a 1,000/day hard maximum, superseding the earlier subscription-sized operating policy. The bot and API facade share one credential; larger advertised subscription allowances do not override the user's spending limits.

**How to apply:** Route every authenticated capability through the shared persistent ledger. Keep every configured bookmaker enabled, but back off each sport/bookmaker pair independently after complete zero-result snapshots; increase the retry delay from 15 minutes to at most 2 hours and clear it immediately when that pair returns data. Pro includes full results, 90-day history, and resolved-props exports (50 export calls/day). Do not expose webhooks/websockets without Streaming Lite or bulk odds-history without Backfill/Enterprise. Scheduled history collection must use a quota-sized rotating batch; never restore a one-target-per-cycle cap or couple it to settlement-grader cadence.