---
name: Delivery dedup claims
description: Rules for provider-aware duplicate suppression around concurrent Telegram delivery
---

Delivery deduplication must use provider-owned opportunity identity: provider, player, sport, market, and direction, with line, source ID, and event ID retained as audit metadata. Source/event IDs may rotate during feed refreshes, so the same-provider logical identity must suppress repeat sends throughout the dedup window even when the displayed line moves. A pre-delivery claim is only a concurrency guard; it must be released when delivery is filtered or fails, because only a successful Telegram delivery should suppress an exact repeat.

**Why:** Claiming before network I/O prevents concurrent scan paths from sending the same alert twice, but retaining failed claims suppresses legitimate retries and confuses attempted delivery with delivered history.

**How to apply:** Keep dedup downstream of evaluation/tracking/grading. Claim atomically immediately before delivery, preserve the claim after successful delivery, and release it on every non-delivery path. Match rotated source/event IDs only within the same provider, direction, and time window. Never replace identity-aware dedup with player-, market-, sport-, or provider-wide cooldowns.

Every callable prop-to-Telegram adapter must enforce the canonical direction-and-evidence gate, including compatibility senders. Removal and market-movement records are monitoring-only and must return before claim or formatting even when other actionable fields are present.

**Why:** Low-level compatibility senders can bypass the active ranked queue; treating movement or removal as a pick reintroduces the exact monitoring-versus-delivery leakage the queue prevents.

**How to apply:** Keep monitoring observations available for persistence and reporting, but allow only genuine OVER/UNDER recommendations with supporting player-result evidence to reach Telegram.

Formatting is part of the delivery boundary: malformed history or provider metadata must not strand a pre-delivery claim or silently suppress a valid pick. Prefer a minimal actionable fallback; otherwise release the claim and record the formatter failure.

**Why:** A formatter exception occurs after the atomic claim but before Telegram I/O, so treating formatting as harmless presentation can turn one bad payload into a delivery outage and suppress retries.

**How to apply:** Wrap user-facing prop formatting after the claim, log the exception, and preserve player, stat, direction, and line in the fallback message.