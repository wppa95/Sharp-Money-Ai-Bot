---
name: Football active-scope exclusion
description: Active player-prop monitoring excludes gridiron, rugby, and Australian-rules football while preserving soccer and historical rows.
---

Active monitoring must reject football-related provider sports before movement
state, persistence, scoring, evidence enrichment, rescans, or delivery. This
includes NFL, NCAAF/CFB/FCS, CFL, AFL/AFLW, gridiron, rugby, and provider
identifiers containing football. Association soccer is not part of this block.

**Why:** The product does not bet football, and allowing those markets into the
pipeline wastes provider quota and downstream processing while producing
repeated scope-block warnings.

**How to apply:** Reuse `is_football_related_sport()` at provider fetch and
active-pool boundaries. Never delete or rewrite historical football records,
and keep AlertScopeFilter available for unrelated scope violations.