---
name: DK/FD player prop normalization
description: How Odds API player_props market outcomes differ from h2h/totals outcomes in DraftKings and FanDuel connectors
---

## Rule
When the Odds API returns `market_key == "player_props"`, outcome fields have a different shape from h2h/totals:

- `outcome["description"]` = player name  ← NOT the selection label
- `outcome["name"]`        = "Over" / "Under"  ← direction, NOT the player name
- `outcome["point"]`       = line value

Constructed `selection = f"{player} {direction}"` (e.g. "Shohei Ohtani Over").
Set `player=` on `MarketSnapshot` for player prop outcomes; `player=None` for h2h/totals.

**Why:** The generic `_normalize()` in both connectors used `outcome["name"]` as the selection for all market types, which works for h2h/totals but silently drops player prop data or produces garbage selections.

**How to apply:** Any new connector that ingests player props from the Odds API must branch on `market_key` before reading outcome fields. The `is_player_prop = market_key == "player_props"` flag pattern is the established approach (see `bot/connectors/draftkings.py` and `bot/connectors/fanduel.py`).
