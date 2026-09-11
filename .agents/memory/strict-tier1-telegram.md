---
name: Telegram sport scope
description: Football delivery exclusions and canonical evidence requirements for actionable alerts.
---

The monitoring and evaluation pipeline continues ingesting and tracking every supported sport. Telegram delivery blocks NFL and related gridiron sports (including college football, CFL, and FCS), while MLB and every non-football sport remain eligible. Eligible player picks still require an explicit OVER or UNDER direction plus sufficient evidence/history; stars, score, confidence, Bet Quality, Market Quality, and S/A/B/C tier remain internal/reporting metadata and must not be rendered in Telegram delivery. Existing safety, live-game, deduplication, freshness, scope, rate-limit, and validity protections remain operational safeguards.

**Why:** The user explicitly requires NFL and related football sports to remain blocked from Telegram, while MLB and all non-football sports should alert when backed by legitimate evidence. Quality metadata must not silently suppress otherwise valid eligible-sport picks.

**How to apply:** Use the delivery-only disabled-sports configuration for NFL/NCAAF/CFB/CFL/FCS; do not suppress their ingestion, history, grading, or reporting. Keep MLB enabled. Preserve tier and quality values for ranking, logs, database records, and diagnostics, but strip them from Telegram-facing alert text. For settled-result evidence, require every L5/L10/L20/L30/season window to be at ≥70% OVER rate or ≤40% OVER rate for UNDER before delivery. Render STRONG BET for either supported direction, and never use the Telegram gate to stop underlying evaluation.