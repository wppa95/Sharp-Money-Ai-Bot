---
name: Completed-game source boundaries
description: Research constraints for adding completed player-game history from external sports feeds.
---

Require a documented player/event identity and a confirmed completion state before using an external record as settled game history. For Global Baseball, a player-summary endpoint is limited to the 10 most recent games; longer history needs event-level collection. For PandaScore, wait for `finished` and `complete=true` before treating detailed post-game statistics as final. Sportradar’s table-tennis and badminton documentation establishes completed match outcomes and set scores, but does not establish richer player-stat coverage.

**Why:** Broad product statements such as “statistics when available” do not prove that a specific prop field is returned for every competition or event. A finished score can be suitable for winner/set history while remaining insufficient for a player-stat market.

**How to apply:** Before integrating a provider, confirm the exact competition in its coverage matrix and inspect an authorized sample response for required fields, stable IDs, final status, history depth, quota, and usage rights. Use the research report and saved source registry as starting evidence, then re-check current provider documentation.