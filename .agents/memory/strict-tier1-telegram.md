---
name: Sport-neutral Telegram policy
description: Canonical sport-neutral monitoring and delivery requirements for Underdog actionable alerts.
---

The monitoring and evaluation pipeline treats every supported sport and quality level uniformly for alert qualification. Telegram delivery requires an explicit OVER or UNDER direction, in addition to existing safety, live-game, deduplication, freshness, scope, rate-limit, and validity checks.

**Why:** The user explicitly removed the retired S/A/B/C, confidence, evidence, BQ, MQ, and sport-tier qualification framework; retaining any of those as a delivery gate would silently suppress valid directional picks.

**How to apply:** Keep the rule centralized at the canonical delivery boundary. Treat tier and quality values as optional display/reporting metadata only, and never use the Telegram gate to stop underlying evaluation.