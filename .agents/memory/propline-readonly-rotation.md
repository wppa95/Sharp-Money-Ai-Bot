---
name: PropLine read-only rotation
description: Durable rotation rules for supplemental PropLine endpoint coverage.
---

The supplemental PropLine endpoint families use a bounded scheduler lane with
stable, discovery-backed target identities and an atomic SQLite cursor. The
lane must share the existing persistent request limiter and leave explicit
capacity for the full player-history collector; a restart may repeat the last
request but must not skip a family or target.

Raw `player_games` rows are completed-history evidence, not a general market
fallback. Enable them only for discovered sports and documented raw-stat fields;
the shared `esports` catalog key supports CS2 stats only, not Dota, LoL, or
Valorant. Do not move raw-game calls into the fast player-history collector.

**Why:** PropLine exposes useful read-only evidence across several endpoint
families, but independently scheduling every family can exhaust the shared
rolling-hour quota or create duplicate coverage after a restart.

**Why:** Raw game-stat vocabularies can be wider than the market catalog, and a
shared provider sport key can make unsupported esports titles look eligible.

**How to apply:** Build targets only from discovered official sports, active
player/stat inputs, and official event IDs. Keep raw-stat allowlists explicit,
preserve completion and event identity checks, and keep supplemental responses
out of alert scoring. Only explicit won/lost/push/void outcomes with complete,
non-terminal evidence may become settlements; report all other outcomes as
pending with provenance.