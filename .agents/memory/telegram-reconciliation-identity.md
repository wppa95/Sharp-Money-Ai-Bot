---
name: Telegram reconciliation identity
description: Durable identity requirements for reconciling outbound Underdog Telegram sends with stored picks.
---

Underdog delivery audits must retain the provider source identity and originating delivery path; player/stat/line alone is not a deterministic join key.

**Why:** A historical send can be proven successful at the Telegram API layer while remaining impossible to join to the exact opportunity when external_id is omitted and multiple delivery paths share one label.

**How to apply:** Treat audit delivery identity, provider-scoped opportunity identity, and the successful sent-mark update as one atomic reconciliation contract. Reports should distinguish API sends from tracked opportunity rows.