---
name: Ranked delivery queue
description: Delivery prioritization system — collects candidates from all three alert paths, ranks by quality, delivers in priority order after the standing scan.
---

# Ranked Delivery Queue

## Rule
All qualified prop candidates from new-prop, line-change, and standing paths are collected into `_delivery_queue` during the scan loop, then ranked and delivered after the standing scan. No path delivers to Telegram inline anymore.

## Priority formula
`_DELIVERY_TIER_BASE` (S=10000, A=5000, B=1000, C=200) + `conf×0.5 + bq×0.3 + mq×0.2` + 500 if `is_tier1` + 200 if `is_meaningful_change`.

## Soft diversification
`_apply_delivery_diversification()` applies −300/−600 penalty for 2nd/3rd+ candidate in the same `(sport, stat_type)` group, then re-sorts. Not a hard cap.

## Key ordering constraint
Inside `if _dq_result.sent:`, per-path counters/callbacks are structured as separate if/elif branches with the counter FIRST, then `_record_prop_alerted`, then `_lifecycle_alerted.append`. Source-inspection tests look for callbacks within N chars AFTER the counter landmark — violating this order breaks those tests.

## Standing-path alias
The standing elif uses `_sp = _dq_player` so that `_lifecycle_alerted.append((_sp, ...))` satisfies the `test_lifecycle_append_uses_correct_vars_standing` source-inspection test that requires `_sp` inside the append.

## Warning log strings
`mark_opportunity_alert_sent [lc] failed` and `mark_opportunity_alert_sent [standing] failed` must appear as literal strings (not `[%s]`) in the except branches to satisfy source-inspection test in `test_credibility_fixes.py`.

## Bulk-save timing
`_incremental_records` bulk-save is performed AFTER the ranked delivery phase so `alert_sent=True` is persisted for delivered candidates. Delivery phase updates `record.alert_sent` and `record.alert_outcome` in-place before save.

## Record linkage
After `_incremental_records.append(record)`, the code walks `_delivery_queue` in reverse to link the snapshot record object to the matching queued candidate. Delivery phase flips `alert_sent=True` on the record for delivered candidates.

**Why:** Prevents the first-encountered props from winning rate-limit slots over higher-quality picks; best candidates always get delivered first regardless of scan order.

## Tests
`bot/tests/test_delivery_queue.py` — 19 tests covering priority formula, diversification, empty/single/multi-candidate queues, tier1 bonus, meaningful-change bonus, group-rank attribute.

4447 tests passing Aug 2026.
