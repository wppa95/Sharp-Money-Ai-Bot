---
name: PropLine Pro quota boundary
description: Subscription capability and shared quota rules for every PropLine client process.
---

All authenticated PropLine traffic must share a cross-process, restart-safe limit of 1,020 requests per rolling hour and 25,000 requests per UTC day. Provider daily-quota headers are telemetry, not hourly pacing signals. Provider Retry-After cooldowns apply across processes.

**Why:** The bot and API facade run as separate processes against one Pro credential. Independent in-memory counters can overspend the subscription, and the public freshness endpoint advertises a free-tier daily limit that must not be mistaken for an hourly cap.

**How to apply:** Route every authenticated capability through the shared persistent ledger. Keep every configured bookmaker enabled, but back off each sport/bookmaker pair independently after complete zero-result snapshots; increase the retry delay from 15 minutes to at most 2 hours and clear it immediately when that pair returns data. Pro includes full results, 90-day history, and resolved-props exports (50 export calls/day). Do not expose webhooks/websockets without Streaming Lite or bulk odds-history without Backfill/Enterprise.