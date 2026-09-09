---
name: Odds API player-prop feed
description: Odds API is a sportsbook player-prop supplement to the canonical Underdog workflow.
---

Odds API polling is player-prop-only. DraftKings and FanDuel observations are persisted through the shared canonical PropLineHistory boundary and may enrich the Underdog market view, but they do not create an independent sportsbook alert path. Underdog still owns actionable direction, delivery, lifecycle, grading, and settlement.

**Why:** General h2h/totals polling consumed quota without serving the player-prop product, while sportsbook player props provide useful current market context.

**How to apply:** Add sportsbook rows as source-owned observations with stable event/player/stat identity. Keep line movement as new canonical observations for the same source identity, and do not deliver a sportsbook row unless the Underdog workflow has the actionable selection.