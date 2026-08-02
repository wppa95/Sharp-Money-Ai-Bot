---
name: Alert Quality Cleanup Batch
description: Six alert quality fixes: intelligence block char-split, confidence language calibration, tier/confidence consistency gate, role risk adjustment, Market vs Bet Quality separation.
---

## What was fixed

### 1. Intelligence block character-splitting bug (alerts_multiplatform.py)
`matchup.reasoning` is stored as a **string** in the prop_intelligence trace (set
by `compute_matchup_intelligence()`). The old `for rsn in list(match_rsns)[:3]`
iterated over individual characters when given a string → "N", "e", "u" instead
of "Neutral".

Fix: detect type; if string → show as one bullet; if list → show only first element.

**Why:** The `intelligence_trace` serialization in `prop_intelligence.py` stores
`"reasoning": matchup.reasoning` (a plain string). It was never a list in
production. The old code worked accidentally only when reasoning was empty.

### 2. Analyst language calibration (engine/analyst.py)
Added `_confidence_label(conf: int) -> str`:
- 95+ → "Elite confidence signal"
- 80-94 → "High-confidence signal"
- 65-79 → "Strong but monitored signal"
- 55-64 → "Moderate signal"
- <55 → "Low-confidence signal"

`build_analyst_from_alert_parts()` now uses `_confidence_label(confidence)` instead
of `_tier_label(decision_tier)` for the lead sentence. This prevents "highest-
confidence" language appearing when confidence is 79/100.

### 3. Final tier validation gate (alerts_multiplatform.py)
`_validate_final_tier(tier, conf, intelligence_trace) -> str`:
- S-tier requires conf ≥ 80 (otherwise → A)
- S-tier + Bench role requires conf ≥ 90 (otherwise → A)
- A-tier + Bench + Volatile + conf < 65 → B

Called inside `_format_analyst_inline_block()`. When downgraded, a `⚠️ Tier adjusted
X→Y` note is appended to the analyst block. Never upgrades; only downgrades.

### 4. Market Quality vs Bet Quality separation (alerts_multiplatform.py)
`_format_market_quality_block()`: added subtitle "How reliable is the market data?"
`_format_decision_block()`: Confidence line now reads "Bet Quality" with subtitle
"how strong is the actual recommendation". Separates informational market-data
context from actionable recommendation strength.

### 5. Role risk (existing + gate)
The existing `_intelligence_adjusted_tier()` in `candidate.py` already does a
1-step downgrade for Bench+Volatile. The new gate adds a second enforcement layer
at display time, catching cases where the UDBetDecision tier was set before the
prop intelligence was applied.

## Files changed
- `bot/alerts_multiplatform.py` — _format_intelligence_block fix, _validate_final_tier, _format_analyst_inline_block gate, _format_decision_block Bet Quality label, _format_market_quality_block subtitle
- `bot/engine/analyst.py` — _confidence_label, updated build_analyst_from_alert_parts
- `bot/tests/test_alert_quality.py` — 39 new tests
- `bot/tests/test_analyst_inline.py` — updated 1 test for string reasoning
- `bot/tests/test_intelligence_alert.py` — updated 1 test for string reasoning

## Test count
2634 passing after batch.
