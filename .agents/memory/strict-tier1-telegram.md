---
name: Sport-neutral Telegram policy
description: Canonical sport-neutral monitoring and delivery requirements for Underdog actionable alerts.
---

The monitoring and evaluation pipeline treats every supported sport and quality level uniformly for alert qualification. For player-pick Telegram delivery, an explicit OVER or UNDER direction plus sufficient evidence/history is required; stars, score, confidence, Bet Quality, Market Quality, and S/A/B/C tier remain internal/reporting metadata and must not be rendered in Telegram delivery. Existing safety, live-game, deduplication, freshness, scope, rate-limit, and validity protections remain operational safeguards.

**Why:** The user explicitly required evidence/history to be the only player-pick qualification gate and required supported OVER and UNDER picks to render STRONG BET regardless of quality metadata; retaining the former star gate silently suppressed valid directional picks.

**How to apply:** Keep the rule centralized at the canonical delivery boundary. Preserve tier and quality values for ranking, logs, database records, and diagnostics, but strip them from Telegram-facing alert text. For settled-result evidence, treat any L5/L10/L20/L30/season window at ≥70% OVER rate or ≤40% OVER rate for UNDER as strong directional support. Render STRONG BET for either supported direction, and never use the Telegram gate to stop underlying evaluation.