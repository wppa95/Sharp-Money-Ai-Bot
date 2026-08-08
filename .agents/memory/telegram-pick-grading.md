---
name: Telegram pick grading (alert_sent)
description: Separates Telegram actionable picks from all evaluated props in PropOpportunityLog; alert_sent field + performance query; 3 engine mark sites.
---

## Rule
Only 🎯 ACTIONABLE BET PICK alerts that pass `deliver_underdog(sent=True)` receive `alert_sent=True`. Market moves, PASS, blocked, filtered, and removal alerts are never marked.

## Schema changes
- `PropOpportunityLog.alert_sent` — `Boolean NOT NULL DEFAULT 0`
- `PropOpportunityLog.alert_sent_at` — `DateTime nullable`
- Migration: `_migrate_prop_opportunity_log_v4()` called from `Database.initialise()`

## New DB methods
- `mark_opportunity_alert_sent(external_id, stat_type)` — UPDATE WHERE (external_id, stat_type); safe no-op if row missing.
- `get_telegram_pick_performance()` — returns `{total, hit, miss, push, pending, graded, hit_rate}` filtered to `alert_sent=True AND recommendation IN (OVER, UNDER)`.

## Engine call sites (market_engine.py)
Three paths — all wrapped in `try/except` so they never block alert flow:
1. New-prop path: after `if ud_result.sent: _n_new_prop_sent += 1`
2. Line-change path: inside `if ud_result.sent and not is_removed:` block (not called for removals)
3. Standing path: after `if _sresult.sent: _n_standing_sent += 1`

## Reporting
`/rollups` now shows 🎯 TELEGRAM ACTIONABLE PICKS block at the top (from `get_telegram_pick_performance()`), followed by the existing overall grading section. Both views coexist; overall grading unchanged.

## Key invariants
- `on_conflict_do_update` in `log_prop_opportunity` does NOT include `alert_sent` → re-evaluation never resets it.
- `alert_sent_at` also excluded from on_conflict set_ → timestamp preserved after upsert.
- `grade_opportunity()` does not touch `alert_sent` → grading works on all rows.
- `get_pending_opportunities()` returns all PENDING rows (not filtered by alert_sent) → overall grading pipeline unaffected.
- `SlipJournalLeg.opp_id → PropOpportunityLog.id` relationship intact (primary key unchanged).

**Why:** alert_sent=False for all evaluated props (PASS, blocked, market-move) means the performance query gives a clean Telegram-only hit rate without mixing in pipeline noise.
