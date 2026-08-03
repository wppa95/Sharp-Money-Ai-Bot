---
name: Phase 4 Evidence Infrastructure
description: PropCandidateLog table, PropOpportunityLog Phase 4 columns, /funnel command, reason-code helpers — completed Aug 2026.
---

## What was built

### New DB table: `PropCandidateLog`
Every scored prop candidate (qualifying or rejected) is written here once per scan cycle.
`gate_decision` values: ACCEPTED / WATCHLIST / REJECTED / REMOVED.
Written by `db.log_prop_candidate_batch()` called from `market_engine.underdog_job` after the debug summary.

### New `PropOpportunityLog` columns (migration v3, idempotent)
recommendation_id, provider, bet_quality_score, qualification_path, reason_codes,
watchlist_state, settlement_source, manual_opinion.
`recommendation_id` = `sha256(f"{external_id}:{stat_type}")[:16]` — stable across re-evaluations.

### New DB methods
- `log_prop_candidate_batch(candidates: list[dict]) -> int`
- `get_funnel_summary(since_hours=24) -> dict` — returns gate counts + top 8 near-miss rejections.
- `_migrate_prop_opportunity_log_v3()` — idempotent ALTER TABLE for all 8 Phase 4 columns.

### New market_engine.py helpers
- `_compute_reason_codes(score, decision) -> list[str]` — object-based (used at log_prop_opportunity call sites)
- `_compute_reason_codes_from_scored_dict(p: dict) -> list[str]` — dict-based (used for PropCandidateLog batch)

### /funnel command
`cmd_funnel` in commands.py; registered in main.py.
Optional arg: `hours` (1–168), default 24.
Shows: total scanned, accepted/watchlist/rejected/removed counts, qualification rate, top 8 near-miss rejections.

## Key decisions
**Why:** Prop alerts were a black box — no way to know what volume of candidates was evaluated or where they dropped out.
**How to apply:** These tables are append-only; never rely on them for alert flow logic. Read-only funnel analysis only.

## gate_decision mapping rule (PropCandidateLog)
- tier == "PASS"               → REJECTED
- tier == "B"                  → WATCHLIST
- tier in ("S","A") + no rejection → ACCEPTED
- tier in ("S","A") + has rejection → REJECTED

## Test count after Phase 4: 2685 passing (Aug 2026)
