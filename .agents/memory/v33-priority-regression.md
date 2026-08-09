---
name: V3.3 priority override regression fix
description: Root cause and fix for MLB UNDER alerts firing via the 95+ BQ override path + message showing score.total instead of BQ.
---

## Root Cause

Three bugs combined to produce the "🔥🚨 S-TIER PRIORITY OVERRIDE — 52/100" MLB UNDER alerts:

1. `_format_95_priority_alert` displayed `score.total` (e.g. 52) as the header number and said
   "Score ≥ 95/100 — immediate priority override. All validation gates bypassed." — wrong field, inaccurate wording.

2. All three override blocks (new-prop, lc, standing) fired `broadcast_alert` BEFORE the MLB UNDER
   gate in the normal gate sequence, so MLB UNDER props bypassed the direction rule.

3. The spec requires 95+ BQ to still enforce: Sport Direction (MLB/NFL OVER-only) + Confidence Gate
   (implicitly met at BQ≥95) + Quality Gate (implicitly met at BQ≥95) + Dedup/Reversal.

## Fix (V3.3)

**`bot/market_engine.py`** — 4 edits:
- `_format_95_priority_alert`: changed header from `{int(score.total)}/100` → `{conf}/100`;
  replaced "Score ≥ 95/100 — immediate priority override. All validation gates bypassed."
  with "🔥 Bet Quality {conf}/100 — Priority".
- All 3 override blocks: added `_np_95_dir_ok / _lc_95_dir_ok / _sp_95_dir_ok` = `not (sport in {"MLB","NFL"} and recommendation == "UNDER")` check BEFORE broadcasting.

**`bot/tests/test_priority_override.py`** — tests updated:
- `test_message_contains_score` → `test_message_contains_bet_quality` (checks conf not score.total)
- `test_message_includes_bypass_notice` → `test_message_shows_priority_label`
- `test_message_says_bet_quality_not_score_gte` added
- `_route` helper updated with sport-direction layer (MLB/NFL UNDER → DIRECTION_BLOCKED)
- New `TestV33SportDirectionPolicy` class with 18 spec cases

**Why:**
MLB `decision.confidence` can reach 95 (game-history hit rates) while `score.total` stays at 52
(raw UD market score). The priority gate must check `decision.confidence`, but the direction block
(MLB/NFL OVER-only) must still run even when BQ≥95 clears the threshold.

**How to apply:**
Any future priority gate using `decision.confidence >= 95` must include the direction check before
broadcasting. The three variable names (`_np_95_dir_ok`, `_lc_95_dir_ok`, `_sp_95_dir_ok`) are in
each path's override block.
