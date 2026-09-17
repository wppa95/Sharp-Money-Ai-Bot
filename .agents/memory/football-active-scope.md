---
name: Football active-scope exclusion
description: Active player-prop monitoring excludes gridiron, rugby, and Australian-rules football while preserving soccer and historical rows.
---

User-facing actionable delivery must reject football-related provider sports
before scoring output or Telegram delivery. This includes NFL,
NCAAF/CFB/FCS, CFL, AFL/AFLW, gridiron, rugby, and provider identifiers
containing football. Association soccer is not part of this block. Provider
observations may still be retained for persistence and historical diagnostics.

**Why:** The product does not bet football, and football-family rows were
reaching the Underdog delivery path even though the classifier already
recognized them. Blocking at the final alert boundary prevents user-facing
picks without deleting evidence or changing historical reports.

**How to apply:** Reuse `is_football_related_sport()` in every actionable
Telegram path, including direct pregame-watch broadcasts. Keep provider
observations and historical football records intact, and keep AlertScopeFilter
available for unrelated scope violations.