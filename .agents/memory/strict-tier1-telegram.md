---
name: Telegram sport scope
description: Telegram sport eligibility and canonical evidence requirements for actionable alerts.
---

The monitoring and evaluation pipeline continues ingesting and tracking every supported sport. No supported sport is excluded from Telegram solely because of its sport label, including football-family aliases such as NCAAF, CFL, and FCS. Eligible player picks still require an explicit OVER or UNDER direction plus sufficient evidence/history; primary game results and explicitly settled PropLine results use the same five-sample minimum. Stars, score, confidence, Bet Quality, Market Quality, and S/A/B/C tier remain internal/reporting metadata and must not be rendered in Telegram delivery. Existing safety, live-game, deduplication, freshness, scope, rate-limit, and validity protections remain operational safeguards.

**Why:** The user explicitly expanded Telegram eligibility to all supported sports. Valid picks in any sport should not be suppressed just because the provider labels them as football-family, while evidence and operational safety checks remain in force.

**How to apply:** Keep football-family classification for normalization and diagnostics, but do not use it as a Telegram delivery veto for supported sports. Preserve direction, completed-history source/sample, timing, freshness, tracking, deduplication, and explicit operator-disable checks. Preserve tier and quality values for ranking, logs, database records, and diagnostics, but strip them from Telegram-facing alert text. Render STRONG BET for either supported direction, and never use the Telegram gate to stop underlying evaluation.