---
name: Telegram reconciliation identity
description: Durable identity requirements for reconciling outbound Underdog Telegram sends with stored picks.
---

Delivery audits must retain provider source identity and originating path; player/stat/line alone is not a deterministic join key. Set `alert_sent` only from a confirmed successful ACTIONABLE_PICK audit matched to the exact provider, source, player, sport, event, market, original line, and direction. Before sending queued work, revalidate both the latest source observation and current directional opportunity. No independent transport-success flag writer.

**Why:** A historical send can be proven successful at the Telegram API layer while remaining impossible to join to the exact opportunity when source identity is omitted or multiple lines/events share a label. Directly setting the legacy flag can claim delivery without an exact successful audit; delayed queue work can also outlive its qualified source or direction.

**How to apply:** Treat audit delivery identity, provider-scoped opportunity identity, source revalidation, and the successful sent-mark update as one atomic reconciliation contract. Do not repair historical flags from transport logs alone; reports should distinguish API sends from tracked opportunity rows.