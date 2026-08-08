---
name: V3.2 final targeted fix pass
description: 8 targeted fixes — cold_start gate_decision, /picks policy enforcement, /alerts canonical source, funnel rate precision, dashboard tier % denominator.
---

## Fix 1 — cold_start S/A gate_decision (market_engine.py)

cold_start S/A props were stored as gate_decision="REJECTED" in PropCandidateLog,
making them appear in near-misses. Added "cold_start" to the acceptance list:
  `elif _crej in ("qualified", "sent", ..., "cold_start") and _ctier in ("S", "A"):`
Now stored as gate_decision="ACCEPTED". Telegram delivery is still suppressed during
cold_start — only the PropCandidateLog display label changed.

## Fix 2 — /picks strict-sport tier enforcement (commands.py)

`get_top_ud_props_for_picks` returned all non-PASS UD props, showing MLB A-tier
(Yandy Díaz) even though MLB/NFL require S-tier for alerts. Added Python filter
in `_cmd_picks_inner` that mirrors the delivery pipeline:
  `sport not in strict_sports OR score_tier == "S"`

## Fix 3 — /alerts canonical source (database.py + commands.py)

`/alerts` used `PropLineHistory.lifecycle_state == "ACTIVE_ALERTED"` — broken because
each new scan cycle creates a new PropLineHistory row with lifecycle_state=None,
overwriting the ACTIVE_ALERTED state for the latest-row query.

Added `get_alerted_opportunity_log(since_hours, limit)` to database.py — queries
PropOpportunityLog.alert_sent=True which is write-once and never overwritten.
`cmd_alerts` now uses this method. Telegram-delivery count now consistent with /stats.

**Why:** PropLineHistory.lifecycle_state tracks the most-recent snapshot only;
PropOpportunityLog.alert_sent is the permanent delivery record.

## Fix 4 — /funnel clarification

Added explanatory note under qualification rate:
"Qualified = passed scoring gates (S/A-tier). Delivered alerts go through additional
gates (direction, BQ, conf, dedup, live-game). Use /alerts to see Telegram-delivered picks."

Label changed from "Qualified candidates" to "Qualified (S/A-tier)".

## Fix 5 — /health stale errors (already done in prior pass)

Both timestamp parsing sites in cmd_health strip " UTC" suffix. Verified intact.

## Fix 6 — /restarts absent (already done in prior pass)

Confirmed no cmd_restarts in commands.py.

## Fix 7 — /funnel qualification rate precision (commands.py)

`:.0f` format rounded 0.018% → "0%". Replaced with `_fmt_rate(num, denom)` helper
that uses 4 decimals for <0.01%, 3 for <0.1%, 2 for <1%, 1 for ≥1%.
Same fix applied to sport-row pass percentages.

## Fix 8 — Dashboard tier breakdown denominator (engine/dashboard.py)

`total_ud_alerts` = ALL UnderdogSnapshotRecord rows (~103,894 scanned), but the
tier breakdown only covers alert_sent=True rows (~482). Dividing by 103,894 made
every tier show 0%.

Fixed: `_tier_total = sum(self.ud_tier_breakdown.values()) or 1`
Now A=129/482=26.8% shows as "26.8%" not "0%".

## Test count
3474 passing Aug 2026.
