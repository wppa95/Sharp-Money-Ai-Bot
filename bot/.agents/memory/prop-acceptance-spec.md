---
name: Final Prop Acceptance Spec implementation
description: Covers the ruleset freeze for alert direction gates, UNDER market restrictions, A-tier confidence thresholds, and Strong UNDER signal labeling.
---

## Rules implemented (frozen as of Aug 2026)

### Tier 2 (MLB + NFL)
- Both OVER and UNDER are allowed at S/A tier
- **MLB UNDER**: restricted to whitelist markets only (see `config.mlb_under_allowed_markets`)
  - Allowed: Strikeouts, Pitcher Strikeouts, Pitching Outs, Hits Allowed, Earned Runs Allowed, Earned Runs, Walks Allowed, Walks, Fantasy Points, Runs
  - All other MLB UNDER props → blocked (watchlist)
- **NFL UNDER**: fully allowed on all markets (no whitelist restriction)
- B/C tier → watchlist regardless of direction

### Tier 1 (NBA, NHL, WNBA, TENNIS, SOCCER, MLS, CS, etc.)
- A-tier actionability threshold: **70/100** (was 60) — set via `UD_NON_STRICT_MIN_CONF_A`
  - 70 = actionable, 69 = watchlist (hard boundary)
- OVER and UNDER both fully allowed on all markets

### Strong UNDER signal label
- **🔥 STRONG UNDER** (standalone line) = Tier 1 + UNDER + `decision.confidence >= 70`
- MLB/NFL (Tier 2) UNDER props NEVER show the STRONG UNDER label
- Inline priority label (`_bq_priority_label`): Tier 1 UNDER BQ≥80 → "💪 STRONG UNDER"; Tier 2 UNDER → "💪 STRONG BET"

## Key code locations
- `config.mlb_under_allowed_markets` property — whitelist definition
- `config.is_mlb_under_allowed(stat_type)` — case-insensitive whitelist check
- `config.UD_NON_STRICT_MIN_CONF_A` — now default 70 (was 60)
- `market_engine.py` — 5 gate points updated: new-prop path, new-prop 95+ override, LC `_lc_mlb_ok` + `_lc_strict_tier_ok`, LC 95+ override (`_lc_95_dir_ok`), standing path, standing 95+ override
- `alerts_multiplatform.py` — `_bq_priority_label(bq, direction, sport)` + `strong_under_line` block in both change-alert and new-prop-alert functions

## Important scoping fix
`_lc_odds_confirm` must be initialised at the **if/else preamble block** (~line 1228 in market_engine.py, alongside `ud_result` init) — NOT inside the `else:` LC branch. The `if ud_result.sent:` lifecycle block at 12-space scope is OUTSIDE the 16-space LC else branch, so any variable only set inside `else:` would be unbound when the new-prop `if:` branch ran instead.

**Why:** The new-prop `if is_new_prop:` and the LC `else:` are sibling branches. `if ud_result.sent:` sits at the same level as both siblings (12 spaces), running after either branch. Variables defined only inside `else:` are unbound when new-prop path runs.

## Test coverage
- `bot/tests/test_prop_acceptance_spec.py` — 45 tests covering all 27 spec boundaries
- Pre-existing failures (not caused by spec implementation): test_analyst_inline (2), test_v34_star_system::TestBqPriorityLabel (6), test_cred_burning_protection::TestDeduplication (2) + test_24 (1)
- Suite: 4259 passing after implementation
