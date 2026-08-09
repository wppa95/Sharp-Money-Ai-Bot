---
name: V3.2 Final Tier 1 Fix
description: Three targeted fixes restoring generic Tier 1 policy — /picks B-tier, np_immediate stars, PropCandidateLog classification.
---

## Fixes applied (Aug 2026 — 3,817 tests passing)

### Fix A — /picks DB query: B-tier allowed for Tier 1 (non-MLB/NFL)
**Rule:** `get_top_ud_props_for_picks()` must use `or_(score_tier.in_(["S","A"]), and_(score_tier=="B", sport.notin_(["MLB","NFL"])))` not a flat `score_tier.in_(["S","A"])`.

**Why:** The alert engine allows B-tier for Tier 1 (line-change/standing paths use `decision.decision_tier in ("S","A","B","C")` for non-strict sports). The flat S/A filter was inadvertently applying Tier 2 strictness to Tier 1 /picks, hiding B-tier Tier 1 props that were already alerting on Telegram.

**How to apply:** MLB/NFL always stay S/A-only in /picks. Any non-MLB/NFL sport with score_tier="B" is now visible. NULL/PASS still excluded.

### Fix B — np_immediate: use min_stars_for_sport() not UD_MIN_STARS_TO_ALERT
**Rule:** `np_immediate` condition must use `config.min_stars_for_sport(snap.sport or "")` not `config.UD_MIN_STARS_TO_ALERT`.

**Why:** `UD_MIN_STARS_TO_ALERT = 3` (strict threshold, correct for MLB/NFL). `UD_NON_STRICT_MIN_STARS = 2` (Tier 1 floor). A 2-star CS/LOL/WNBA new prop was never `np_immediate=True` via the stars branch, so it never reached `make_ud_bet_decision()` and always got `_np_rej = "not_immediate"`. The line-change and standing paths already used `min_stars_for_sport()` correctly — only the new-prop path was wrong.

**How to apply:** `min_stars_for_sport("MLB") == 3`, `min_stars_for_sport("CS") == 2`. Values unchanged.

### Fix C — PropCandidateLog: Tier 1 B-tier qualifying props → ACCEPTED
**Rule:** B-tier classification must check sport: `if not is_strict_sport and _crej in _accepted_rejections → ACCEPTED; else → WATCHLIST`.

**Why:** The flat `elif _ctier == "B": _cgd = "WATCHLIST"` treated B-tier as WATCHLIST for all sports. Tier 1 props that passed the full alert pipeline (is_qualified=True, `_crej = "qualified"`) were classified WATCHLIST instead of ACCEPTED, so /funnel undercounted Tier 1 qualified volume. Near-misses showed REJECTED props only — WATCHLIST was invisible.

**How to apply:** MLB/NFL B-tier → WATCHLIST (unchanged). Non-MLB/NFL B-tier with `_crej in ("qualified","sent","filtered","new_prop_failed","cold_start")` → ACCEPTED.

### Refactoring note
Extracted `_accepted_rejections = ("qualified","sent","filtered","new_prop_failed","cold_start")` as a named variable for reuse across both the B-tier and S/A branches. Old test that searched for `'"cold_start"' in line AND "_crej in" in line` was updated to allow either the inline or variable pattern.

### What remains unchanged
- `ud_strict_alert_sports = frozenset({"MLB","NFL"})` — unchanged
- All threshold values (UD_MIN_STARS_TO_ALERT=3, UD_NON_STRICT_MIN_STARS=2, UD_MIN_CONF_S=80, etc.) — unchanged
- MLB UNDER block — unchanged
- All three alert path strict-sport tier gates — unchanged
- BQ gate — unchanged
- /picks display loop `_strict_sports` filter — unchanged
- `_render_pick_entry` proxy-conf fallback (proxy<30 → use DB tier) — unchanged
- All persistence/dedup mechanisms — unchanged
