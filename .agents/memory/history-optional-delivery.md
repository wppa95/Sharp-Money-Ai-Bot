---
name: History-optional delivery policy
description: Sport-specific player-history requirements for directional Telegram delivery.
---

User-facing picks require explicit OVER/UNDER direction. MLB, NBA, and WNBA always require qualifying history; other non-football sports may proceed only when no history is available, and any available history still needs five settled results. Football-family sports remain blocked.

**Why:** Primary baseball/basketball markets are expected to have history, while sparse or newly supported sports should not disappear solely because a provider has not returned history yet. Available history must never be silently bypassed.

**How to apply:** Enforce direction and football suppression first. For MLB/NBA/WNBA, require history even when absent. For other non-football sports, apply the existing completed-history provenance/sample gate when a marker exists; otherwise allow direction through downstream freshness, timing, tracking, and deduplication checks.