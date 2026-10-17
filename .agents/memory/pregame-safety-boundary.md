---
name: Pregame delivery boundary
description: Where strict event-state safety applies without corrupting historical reporting
---

Production Telegram delivery requires a known pregame event. Qualified individual
picks may alert immediately, without a two-hour maximum or same-reporting-day
restriction. Missing timestamps require an explicit pregame provider state.
Historical alert reports remain independent of the current event state.

**Why:** The user explicitly removed fixed-hours-before-game delays in the
pick-to-delivery follow-up, superseding the prior same-day restriction. They
retained started-event, stale-line, source identity, and delivery safety checks.

**How to apply:** Keep strict pregame validation on actionable Telegram paths,
but do not delay a qualified fresh pick until a calendar day or countdown window.
Keep `/alerts`, grading, and historical reports independent of present event state.