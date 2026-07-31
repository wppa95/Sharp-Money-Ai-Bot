---
name: Player prop picks/slip framework
description: How /picks and /slip work after the PPEdgeRecord migration — data sources, PropPickAdapter, and the build_player_prop_market_comparison threshold override.
---

## Rule
`/picks` and `/slip` no longer use `get_top_pp_edges()` (PPEdgeRecord) as their data source.

**Data flow:**
1. `db.get_top_ud_props_for_picks(limit, since_hours)` — recent Underdog `PropLineHistory` rows, deduped by (player, stat), non-removed.
2. `db.get_latest_props_for_provider("PrizePicks", since_hours=24)` — PP cross-reference.
3. `db.get_recent_player_prop_lines(["DraftKings","FanDuel"], since_hours=4)` — DK/FD OddsRecords; indexed by `(player_lower, sportsbook) → line` using `_build_dk_fd_index()`.
4. `build_player_prop_market_comparison(..., min_confidence=0)` — pass `min_confidence=0` to bypass the threshold gate; confidence is display info only in /picks.
5. Wrap each result in `PropPickAdapter(plh, comp)` before passing to `build_all_slips`.

**PropPickAdapter** lives in `commands.py`. Provides all fields the slip optimizer (`check_correlation`) and `_render_slip_section` need: `player_name`, `sport`, `stat_type`, `team`, `game_description`, `confidence`, `tier`, `best_edge` (movement-derived), `best_side`, `pp_line_value` (= best available line), `prev_line`, `opening_line` (None), `game_time`, `sportsbook`, `result`, `comp`.

**`_render_slip_section`** detects `has_adapters = any(hasattr(r, "comp") for r in records)` and switches to movement-based summary instead of edge% for PropPickAdapter legs. Provider lines block is added per leg.

**Why:** The old PPEdgeRecord pipeline required a PrizePicks line to compute an edge%. The new framework works from Underdog as primary with PP/DK/FD as optional enrichment.

**How to apply:** Any new command that needs ranked props should call `get_top_ud_props_for_picks` + `build_player_prop_market_comparison(min_confidence=0)`. The PPEdgeRecord pipeline (get_top_pp_edges) is retained for backward compat with CLV/performance commands but should not be the primary data source for user-facing picks.
