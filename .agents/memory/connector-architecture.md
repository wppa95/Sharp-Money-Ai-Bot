---
name: Multi-platform connector architecture decisions
description: How DraftKings, FanDuel, and Underdog connectors work and how pick'em isolation is enforced
---

## Rule
DraftKings and FanDuel connectors use The Odds API filtered by bookmaker key (`"draftkings"` / `"fanduel"`). No direct public APIs exist for either book.

Underdog uses the unofficial public API endpoint `/v3/over_under_lines` (no auth required as of mid-2025).

Pick'em isolation is enforced by `MarketSnapshot.is_pickem=True` on all Underdog output. Consensus engine and steam inputs both filter out `is_pickem=True` snapshots before any sportsbook analysis.

**Why:** Mixing pick'em projections into sportsbook consensus would corrupt consensus pricing. The architectural guarantee is: `ConnectorRegistry.fetch_sportsbook()` returns only sportsbook data; `fetch_pickem()` returns only pick'em data.

**How to apply:** Any new pick'em connector must set `is_pickem=True` on all its snapshots and mark `self.is_pickem = True` on the connector class. Never pass pick'em snapshots to `compute_consensus()` or `build_multi_book_steam_inputs()`.
