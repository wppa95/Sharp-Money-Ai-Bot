---
name: Provider enable/disable/remove decisions
description: Current provider ownership and retirement decisions
---

## Underdog
**Status:** Active and primary pick'em source. It owns live projections,
directional evaluation, alert lifecycle, and delivery.

## PropLine / PrizePicks
**Status:** Active supplemental observation and official-evidence source.
PropLine remains behind the shared ingestion, pacing, history, and settlement
boundaries; it does not create a duplicate alert lane.

## DraftKings, FanDuel, and The Odds API
**Status:** Retired. Do not re-enable or add replacement network paths unless
the user explicitly requests a new provider integration with a defined use
case and safety tests. Existing persisted identities and historical rows may
be read for compatibility and reporting only.