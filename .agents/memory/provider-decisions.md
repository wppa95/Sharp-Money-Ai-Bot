---
name: Provider enable/disable/remove decisions
description: Documented rationale for each provider's current status — reference before changing any provider state.
---

## DraftKings + FanDuel
**Status:** `enabled=False` — registered in ConnectorRegistry, never fetched, zero quota, zero noise.

**Decision (Aug 2026):** Leave disabled, do not remove code.
- OddsAPI confirmation layer already provides cross-book consensus for S/A picks (non-blocking, budget-safe).
- Re-enabling DK/FD would add OddsAPI quota cost without marginal signal improvement over the existing confirmation layer.
- Code retained so they can be re-enabled trivially if a concrete use-case emerges (e.g. DK-specific line discrepancy detection).
- Freeze notes say "remove completely if unreliable" — decision is to document and freeze as-is rather than remove.

**Why kept in codebase:** Harmless when disabled; cheaper to re-enable than to re-implement. If Phase 2 identifies a concrete value-add, enable then test.

**How to apply:** Do not re-enable without a documented improvement hypothesis and failsafe test. If they still add no value after testing, remove at that point.

---

## Sleeper
**Status:** Active — `SleeperStatsProvider` supplements ESPN game logs for NFL only.

**Decision (Aug 2026):** Keep active.
- NFL has 1 game/week → per-game data is sparse; Sleeper fills gaps in ESPN gamelog.
- NBA/MLB weekly totals stored shadow-only (not used for grading).
- Pick'em lines confirmed inaccessible (all `/v3/pick_em` endpoints 404).
- 55 tests passing; integrated into the unified stats pipeline.

**How to apply:** If Sleeper API changes break the integration, disable it — ESPN is the primary source and the system degrades gracefully without Sleeper.

---

## OddsAPI
**Status:** Active — shared cache (55s TTL), 500 req/month budget enforced.
- Used for: S/A pick confirmation (non-blocking), season-check (78/78 sport keys), backfill result grading.
- Budget tracker warns at 80% and hard-stops at 100%.

---

## Underdog
**Status:** Active (primary pick'em source).

## PrizePicks
**Status:** Active (secondary reference, cross-provider enrichment in /picks and /slip).
