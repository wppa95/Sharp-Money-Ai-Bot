---
name: Diagnosis pass Aug 2026
description: 12-step mandatory diagnosis pass findings and all fixes applied
---

## Fixes applied (4 total)

1. **`get_top_ud_props_for_picks` NameError** — added `from sqlalchemy import or_, and_`
   inside the function body (not module level). PLH column `removed` uses nullable bool
   so `or_(PLH.removed.is_(None), PLH.removed.isnot(True))` is correct.

2. **`_render_pick_entry` tier display wrong for Tier 1 sports** — `plh.score_confidence`
   does NOT exist on PropLineHistory. `getattr(plh, "score_confidence", None)` always
   returned None so the DB-tier fallback (`if _db_conf is not None`) never fired.
   **Fix:** Derive `_db_conf` from score_tier band midpoint: S=87, A=72, B=57.
   This gives correct tier/stars in /picks for WNBA/CS/LOL/TENNIS etc.

3. **Rejection label uses wrong star threshold for Tier 1** — `_lc_rej` used
   `config.UD_MIN_STARS_TO_ALERT` (=3) for the `below_threshold` branch, misreporting
   Tier 1 props (2★ min) as failing the star gate when the real reason was decision_pass.
   **Fix:** Use `config.min_stars_for_sport(snap.sport or "")` for both the condition and label.

4. **`/funnel` "S/A-tier" label misleading** — ACCEPTED gate now includes B-tier for Tier 1
   sports. Updated label to "S/A/B-tier".

## Key diagnostic findings (all confirmed data issues, not code bugs)

- **Zero alerts = correct behavior**: All directional A-tier picks were `lifecycle_state=REMOVED`
  (games ended/props pulled). No currently active props clear all gates simultaneously.

- **player_game_results coverage**: MLB=58,832 (good), NFL=70, DOTA=12. WNBA=0, Tennis=0,
  CS=0, NPB=0, LOL=0. ESPN provider is called per player per day but returns [] for these sports.
  With hit_rates=None, market bypass fires only when `score_total≥70 AND avg_vs_line_pct≥2%`.

- **Dashboard vs Funnel populations are intentionally different**: /dashboard reads
  UnderdogSnapshotRecord where alert_sent=True (historical alerted snaps); /funnel reads
  prop_candidate_log gate_decision. NOT a bug.

- **S-tier 90+ investigation**: 6 UD snaps score_total≥90 (all-time), all LOL/CS/TENNIS.
  All had bet_rec=PASS (except 1 CS that alerted). Stars gate (min_stars_for_sport) and
  dedup gate are appropriate safety gates. BQ≥95 (Tier 2 only) is the strictest secondary
  gate — market bypass gives confidence=min(score*0.90,85) which may be <95 even for 90+.

## Architecture notes

- Standing path runs only for `_prev.score_tier in ("A","S")`. REMOVED props are skipped.
- Market bypass: `score≥70 AND |avg_vs_line_pct|≥2%` → OVER/UNDER with conf=min(total*0.90,85).
- Funnel: ACCEPTED = gate_decision for S/A-tier non-REMOVED + B-tier Tier 1.
