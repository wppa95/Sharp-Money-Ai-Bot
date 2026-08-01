---
name: Framework Foundation Layers
description: Framework v3.0 Layers 1-4 implementation status, module locations, and extension points for future phases.
---

## What was built (framework foundation)

Four framework modules added to `bot/engine/`:
- `engine/identity.py` — Layer 1: CanonicalPlayer, CanonicalMarket, CanonicalEvent, normalize_player_name(), player_key(), normalize_stat(), event_key()
- `engine/candidate.py` — Layer 2: ConfidenceDimensions (4-dim), Candidate dataclass, factory adapters: candidate_from_ud_decision / candidate_from_ev_opportunity / candidate_from_alert_object
- `engine/explanation.py` — Layer 4: ExplanationService.render(candidate, ExplanationFormat), get_explanation_service() singleton

Two modules extended:
- `providers/base.py` — RecoveryStrategy enum (SKIP/BACKOFF/WAIT/DISABLE) added after FailureType
- `providers/health_monitor.py` — recovery_strategy_for(failure_type, streak) function added
- `engine/health.py` — BotErrorType enum (CODE_FAILURE/DATABASE_FAILURE/CRASH/PROCESSING_FAILURE) added

All exports added to `engine/__init__.py`.

## Contract tests

201 new contract tests in:
- `tests/test_canonical_identity.py`
- `tests/test_candidate_contract.py`
- `tests/test_error_taxonomy.py`
- `tests/test_explanation_service.py`

Total tests: 2020 (all passing, 2026-08-01).

## Key constraints

- All factory adapters are ADDITIVE. Existing UDBetDecision, EVOpportunity, AlertObject objects unchanged.
- Candidate contract: decision ∈ {OVER,UNDER,PASS,BLOCK}, tier ∈ {S,A,B,PASS,BLOCK}, risk_level ∈ {LOW,MEDIUM,HIGH,CRITICAL}
- ExplanationService.render() NEVER recalculates confidence, NEVER pulls live data — only reads from stored decision_trace and decision_reason.
- normalize_player_name() keeps underscores in the regex ([^a-z0-9_ ]) so it's idempotent on already-normalized keys.

## What Layer 5 (Confidence Separation) must do next

Map the three existing confidence engines to the 4 ConfidenceDimensions:
- data_confidence ← proxy_match_confidence from player_prop_market.py + sample size gating from ud_scoring.py
- market_confidence ← UDPropScore (velocity/activity/avg_vs_line/consistency/stability) from ud_scoring.py
- betting_edge ← _compute_confidence() from ud_bet_decision.py (hit-rate deviation, window agreement, H2H)
- overall ← weighted combination of the above three

Currently:
- candidate_from_ud_decision() uses tier-proxy for data_confidence (80/70/60/30) and 50-neutral for market_confidence.
- These will be refined when UDPropScore is plumbed through the candidate factory in the Confidence Separation phase.

## What Layer 6 (Hard Block) must do next

Extend alert_scope_filter._block() to carry:
- duration (int, minutes) — how long the block holds
- review_date (datetime) — when to re-evaluate
- reason_code (str enum) — standardised code for classification

Currently _block() only stamps obj.reason with a string and returns FilterResult(allowed=False, reason=str).

**Why:** Framework requires block persistence so operators can audit and expire blocks.
