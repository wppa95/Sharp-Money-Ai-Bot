---
name: Sport-neutral Telegram policy
description: Canonical sport-neutral monitoring and delivery requirements for Underdog actionable alerts.
---

The monitoring and evaluation pipeline treats every supported sport and quality level uniformly for alert qualification. For player-pick Telegram delivery, an explicit OVER or UNDER direction plus sufficient evidence/history is required; stars, score, confidence, Bet Quality, Market Quality, and S/A/B/C tier are reporting metadata only. Existing safety, live-game, deduplication, freshness, scope, rate-limit, and validity protections remain operational safeguards.

**Why:** The user explicitly required evidence/history to be the only player-pick qualification gate and required supported OVER and UNDER picks to render STRONG BET regardless of quality metadata; retaining the former star gate silently suppressed valid directional picks.

**How to apply:** Keep the rule centralized at the canonical delivery boundary. Treat tier and quality values as optional display/reporting metadata only, render STRONG BET for either supported direction, and never use the Telegram gate to stop underlying evaluation.