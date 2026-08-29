---
name: Delivery dedup claims
description: Rules for provider-aware duplicate suppression around concurrent Telegram delivery
---

Delivery deduplication must use provider-owned opportunity identity: provider, source ID, event ID, player, sport, market, line, and direction. A pre-delivery claim is only a concurrency guard; it must be released when delivery is filtered, rate-limited, or fails, because only a successful Telegram delivery should suppress an exact repeat.

**Why:** Claiming before network I/O prevents concurrent scan paths from sending the same alert twice, but retaining failed claims suppresses legitimate retries and confuses attempted delivery with delivered history.

**How to apply:** Keep dedup downstream of evaluation/tracking/grading. Claim atomically immediately before delivery, preserve the claim after successful delivery, and release it on every non-delivery path. Never replace identity-aware dedup with player-, market-, sport-, or provider-wide cooldowns.