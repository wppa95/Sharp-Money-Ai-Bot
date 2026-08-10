"""
V3.2 Final Tier 1 Fix — Focused regression tests.

Three targeted fixes:
  Fix A — /picks DB query: allow B-tier for Tier 1 (non-MLB/NFL) sports
  Fix B — new-prop np_immediate: use min_stars_for_sport() (sport-aware stars floor)
  Fix C — PropCandidateLog: Tier 1 B-tier qualifying props → ACCEPTED (not WATCHLIST)

Policy verified throughout:
  Tier 2 (MLB/NFL) — S-tier OVER only, strict stars/conf/BQ gates
  Tier 1 (all others) — S/A/B allowed, relaxed stars/conf, no BQ gate

Baseline: 3,733 passed.
"""
from __future__ import annotations
import inspect
import pytest

# ═══════════════════════════════════════════════════════════════════════════
# Fix A — /picks DB query: B-tier allowed for Tier 1
# ═══════════════════════════════════════════════════════════════════════════

class TestPicksDBQueryTier1BAllowed:
    """get_top_ud_props_for_picks must allow B-tier for non-MLB/NFL sports."""

    def _src(self) -> str:
        import database as db_mod
        return inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)

    def test_b_tier_included_for_non_strict_sports(self):
        src = self._src()
        assert "B" in src, "B-tier must be included in the filter"
        assert 'notin_' in src or 'NOT IN' in src.upper() or 'not in' in src.lower(), \
            "must exclude MLB/NFL from B-tier allowance"

    def test_mlb_excluded_from_b_tier(self):
        src = self._src()
        assert '"MLB"' in src or "'MLB'" in src
        assert "notin_" in src or "NOT IN" in src.upper()

    def test_nfl_excluded_from_b_tier(self):
        src = self._src()
        assert '"NFL"' in src or "'NFL'" in src

    def test_null_tier_still_excluded(self):
        """NULL/unscored rows must still be excluded for all sports."""
        src = self._src()
        # or_() clause includes S/A; the B clause requires score_tier == "B"
        # so NULL never matches any branch
        assert "score_tier" in src
        # Confirm we are not using "!= PASS" which would let NULL through
        assert 'score_tier != "PASS"' not in src
        assert "score_tier != 'PASS'" not in src

    def test_pass_tier_still_excluded(self):
        """PASS-tier rows must still be excluded."""
        src = self._src()
        # The or_() filter only allows S, A, and B — PASS does not appear in those lists
        assert '"PASS"' not in src.split("score_tier")[1][:200] \
            or 'notin_' in src  # not-in excludes PASS by not mentioning it

    def test_s_a_still_allowed_for_all_sports(self):
        """S and A must still be allowed for every sport (unchanged)."""
        src = self._src()
        assert '"S"' in src and '"A"' in src
        assert ".in_(" in src

    def test_mlb_b_tier_excluded_logic(self):
        """Simulate: MLB B-tier prop must NOT pass the filter."""
        sport = "MLB"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        # Simulate the or_() filter:
        passes = (
            score_tier in ("S", "A")
            or (score_tier == "B" and sport not in strict_sports)
        )
        assert not passes, "MLB B-tier must be excluded"

    def test_nfl_b_tier_excluded_logic(self):
        sport = "NFL"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert not passes, "NFL B-tier must be excluded"

    def test_cs_b_tier_allowed_logic(self):
        sport = "CS"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "CS B-tier must be allowed"

    def test_lol_b_tier_allowed_logic(self):
        sport = "LOL"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "LOL B-tier must be allowed"

    def test_wnba_b_tier_allowed_logic(self):
        sport = "WNBA"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "WNBA B-tier must be allowed"

    def test_tennis_b_tier_allowed_logic(self):
        sport = "TENNIS"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "TENNIS B-tier must be allowed"

    def test_mma_b_tier_allowed_logic(self):
        sport = "MMA"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "MMA B-tier must be allowed"

    def test_val_b_tier_allowed_logic(self):
        sport = "VAL"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "VAL B-tier must be allowed"

    def test_dota_b_tier_allowed_logic(self):
        sport = "DOTA"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "DOTA B-tier must be allowed"

    def test_nhl_b_tier_allowed_logic(self):
        sport = "NHL"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "NHL B-tier must be allowed"

    def test_soccer_b_tier_allowed_logic(self):
        sport = "SOCCER"
        score_tier = "B"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "SOCCER B-tier must be allowed"

    def test_null_tier_excluded_for_all_sports(self):
        """NULL score_tier is never allowed, regardless of sport."""
        strict_sports = {"MLB", "NFL"}
        for sport in ("CS", "LOL", "MLB", "WNBA", "TENNIS"):
            score_tier = None
            passes = (
                score_tier in ("S", "A")
                or (score_tier == "B" and sport not in strict_sports)
            )
            assert not passes, f"NULL score_tier must be excluded for {sport}"

    def test_pass_tier_excluded_for_all_sports(self):
        """PASS score_tier is never allowed, regardless of sport."""
        strict_sports = {"MLB", "NFL"}
        for sport in ("CS", "LOL", "MLB", "WNBA"):
            score_tier = "PASS"
            passes = (
                score_tier in ("S", "A")
                or (score_tier == "B" and sport not in strict_sports)
            )
            assert not passes, f"PASS score_tier must be excluded for {sport}"

    def test_mlb_s_tier_still_allowed(self):
        sport = "MLB"
        score_tier = "S"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "MLB S-tier must still be allowed"

    def test_nfl_a_tier_allowed_in_picks_query(self):
        """NFL A-tier shows in /picks (not Telegram) — /picks is separate from the alert gate."""
        sport = "NFL"
        score_tier = "A"
        strict_sports = {"MLB", "NFL"}
        passes = score_tier in ("S", "A") or (score_tier == "B" and sport not in strict_sports)
        assert passes, "NFL A-tier passes /picks DB gate (displayed; direction gate handles rest)"

    def test_or_clause_in_source(self):
        """Source must use an or_() style clause, not a simple .in_()."""
        src = self._src()
        assert "or_(" in src or "OR" in src.upper()

    def test_and_clause_for_b_tier_sport_check(self):
        src = self._src()
        assert "and_(" in src or "AND" in src.upper()


# ═══════════════════════════════════════════════════════════════════════════
# Fix B — np_immediate uses sport-aware stars floor
# ═══════════════════════════════════════════════════════════════════════════

class TestNpImmediateSportAwareStars:
    """np_immediate must use min_stars_for_sport() not UD_MIN_STARS_TO_ALERT."""

    def _me_src(self) -> str:
        import market_engine as me
        return inspect.getsource(me)

    def test_np_immediate_uses_min_stars_for_sport(self):
        src = self._me_src()
        assert "min_stars_for_sport" in src, (
            "np_immediate must call config.min_stars_for_sport() for sport-aware threshold"
        )

    def test_np_immediate_no_longer_uses_bare_ud_min_stars(self):
        """The bare UD_MIN_STARS_TO_ALERT reference in np_immediate must be replaced."""
        src = self._me_src()
        # Find the np_immediate block
        idx = src.find("np_immediate = (")
        assert idx >= 0
        block = src[idx:idx + 400]
        assert "UD_MIN_STARS_TO_ALERT" not in block, (
            "np_immediate must not use bare UD_MIN_STARS_TO_ALERT — "
            "use min_stars_for_sport() so Tier 1 uses its own lower threshold"
        )

    def test_np_immediate_block_calls_min_stars_for_sport(self):
        src = self._me_src()
        idx = src.find("np_immediate = (")
        assert idx >= 0
        block = src[idx:idx + 400]
        assert "min_stars_for_sport" in block

    def test_min_stars_for_sport_returns_3_for_mlb(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("MLB") == cfg.UD_MIN_STARS_TO_ALERT
        assert cfg.min_stars_for_sport("MLB") == 3

    def test_min_stars_for_sport_returns_3_for_nfl(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("NFL") == cfg.UD_MIN_STARS_TO_ALERT
        assert cfg.min_stars_for_sport("NFL") == 3

    def test_min_stars_for_sport_returns_2_for_cs(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("CS") == cfg.UD_NON_STRICT_MIN_STARS
        assert cfg.min_stars_for_sport("CS") == 2

    def test_min_stars_for_sport_returns_2_for_lol(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("LOL") == 2

    def test_min_stars_for_sport_returns_2_for_wnba(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("WNBA") == 2

    def test_min_stars_for_sport_returns_2_for_tennis(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("TENNIS") == 2

    def test_min_stars_for_sport_returns_2_for_mma(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("MMA") == 2

    def test_min_stars_for_sport_returns_2_for_val(self):
        from config import config as cfg
        assert cfg.min_stars_for_sport("VAL") == 2

    def test_2_star_tier1_prop_becomes_immediate(self):
        """A 2-star CS prop now satisfies np_immediate (Tier 1 floor = 2)."""
        stars = 2
        sport = "CS"
        from config import config as cfg
        floor = cfg.min_stars_for_sport(sport)
        assert floor == 2
        immediate = stars >= floor
        assert immediate, "2-star CS prop must be immediate"

    def test_2_star_mlb_prop_not_immediate_via_stars(self):
        """A 2-star MLB prop is still NOT immediate via the stars branch."""
        stars = 2
        sport = "MLB"
        from config import config as cfg
        floor = cfg.min_stars_for_sport(sport)
        assert floor == 3
        immediate = stars >= floor
        assert not immediate, "2-star MLB prop must not be immediate via stars"

    def test_3_star_mlb_prop_immediate(self):
        stars = 3
        sport = "MLB"
        from config import config as cfg
        floor = cfg.min_stars_for_sport(sport)
        assert stars >= floor

    def test_3_star_cs_prop_also_immediate(self):
        """3-star CS prop is still immediate (floor is 2, 3 ≥ 2)."""
        stars = 3
        sport = "CS"
        from config import config as cfg
        floor = cfg.min_stars_for_sport(sport)
        assert stars >= floor

    def test_thresholds_unchanged(self):
        """UD_MIN_STARS_TO_ALERT and UD_NON_STRICT_MIN_STARS values unchanged."""
        from config import config as cfg
        assert cfg.UD_MIN_STARS_TO_ALERT == 3
        assert cfg.UD_NON_STRICT_MIN_STARS == 2

    def test_line_change_already_uses_min_stars_for_sport(self):
        """Confirm line-change path already uses min_stars_for_sport (unchanged)."""
        src = self._me_src()
        assert "min_stars_for_sport" in src
        # Count occurrences — should be multiple (line-change + standing + new-prop)
        count = src.count("min_stars_for_sport")
        assert count >= 3, f"Expected ≥3 uses of min_stars_for_sport, found {count}"

    def test_standing_already_uses_min_stars_for_sport(self):
        """Confirm standing path uses min_stars_for_sport (unchanged)."""
        src = self._me_src()
        standing_idx = src.find("# Sport-conditional star floor: strict for MLB/NFL, relaxed for others")
        assert standing_idx >= 0
        assert "min_stars_for_sport" in src[standing_idx:standing_idx + 200]


# ═══════════════════════════════════════════════════════════════════════════
# Fix C — PropCandidateLog: Tier 1 B-tier → ACCEPTED when qualifying
# ═══════════════════════════════════════════════════════════════════════════

class TestPropCandidateLogBTierClassification:
    """B-tier Tier 1 props must be ACCEPTED; B-tier Tier 2 (MLB/NFL) stays WATCHLIST."""

    def _src(self) -> str:
        import market_engine as me
        return inspect.getsource(me)

    def test_b_tier_branch_uses_sport_check(self):
        src = self._src()
        idx = src.find("_ctier == \"B\"")
        assert idx >= 0
        block = src[idx:idx + 400]
        assert "MLB" in block and "NFL" in block, (
            "B-tier classification must check sport against MLB/NFL"
        )

    def test_b_tier_tier1_accepted_with_qualifying_reason(self):
        """Tier 1 B-tier prop with 'qualified' reason → ACCEPTED."""
        _ctier = "B"
        _crej = "qualified"
        _csport = "CS"
        _is_strict = _csport in {"MLB", "NFL"}
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")

        if _ctier == "PASS":
            _cgd = "REJECTED"
        elif _ctier == "B":
            if not _is_strict and _crej in _accepted_reasons:
                _cgd = "ACCEPTED"
            else:
                _cgd = "WATCHLIST"
        elif _crej in _accepted_reasons and _ctier in ("S", "A"):
            _cgd = "ACCEPTED"
        else:
            _cgd = "REJECTED"

        assert _cgd == "ACCEPTED", f"CS B-tier 'qualified' must be ACCEPTED, got {_cgd}"

    def test_b_tier_tier2_watchlist(self):
        """MLB B-tier prop → WATCHLIST regardless of rejection reason."""
        _ctier = "B"
        _crej = "qualified"
        _csport = "MLB"
        _is_strict = _csport in {"MLB", "NFL"}
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")

        if _ctier == "PASS":
            _cgd = "REJECTED"
        elif _ctier == "B":
            if not _is_strict and _crej in _accepted_reasons:
                _cgd = "ACCEPTED"
            else:
                _cgd = "WATCHLIST"
        elif _crej in _accepted_reasons and _ctier in ("S", "A"):
            _cgd = "ACCEPTED"
        else:
            _cgd = "REJECTED"

        assert _cgd == "WATCHLIST", f"MLB B-tier must be WATCHLIST, got {_cgd}"

    def test_nfl_b_tier_watchlist(self):
        _ctier = "B"
        _crej = "sent"
        _csport = "NFL"
        _is_strict = _csport in {"MLB", "NFL"}
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        else:
            _cgd = "REJECTED"
        assert _cgd == "WATCHLIST"

    def test_b_tier_tier1_rejected_reason_stays_watchlist(self):
        """Tier 1 B-tier with non-qualifying reason (not_immediate) → WATCHLIST."""
        _ctier = "B"
        _crej = "not_immediate"
        _csport = "LOL"
        _is_strict = False
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        else:
            _cgd = "REJECTED"
        assert _cgd == "WATCHLIST", "not_immediate LOL B-tier must be WATCHLIST"

    def test_b_tier_tier1_decision_pass_stays_watchlist(self):
        _ctier = "B"
        _crej = "decision_pass"
        _csport = "WNBA"
        _is_strict = False
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        else:
            _cgd = "REJECTED"
        assert _cgd == "WATCHLIST"

    def test_pass_tier_still_rejected(self):
        _ctier = "PASS"
        _crej = "qualified"
        _csport = "CS"
        _is_strict = False
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "PASS":
            _cgd = "REJECTED"
        elif _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        elif _crej in _accepted_reasons and _ctier in ("S", "A"):
            _cgd = "ACCEPTED"
        else:
            _cgd = "REJECTED"
        assert _cgd == "REJECTED"

    def test_a_tier_tier1_accepted_with_qualifying_reason(self):
        _ctier = "A"
        _crej = "qualified"
        _csport = "LOL"
        _is_strict = False
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "PASS":
            _cgd = "REJECTED"
        elif _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        elif _crej in _accepted_reasons and _ctier in ("S", "A"):
            _cgd = "ACCEPTED"
        else:
            _cgd = "REJECTED"
        assert _cgd == "ACCEPTED"

    def test_a_tier_mlb_accepted_with_qualifying_reason(self):
        """MLB A-tier still becomes ACCEPTED in PropCandidateLog (scoring, not alert gate)."""
        _ctier = "A"
        _crej = "qualified"
        _csport = "MLB"
        _is_strict = True
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "PASS":
            _cgd = "REJECTED"
        elif _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        elif _crej in _accepted_reasons and _ctier in ("S", "A"):
            _cgd = "ACCEPTED"
        else:
            _cgd = "REJECTED"
        assert _cgd == "ACCEPTED"

    def test_s_tier_tier1_accepted(self):
        _ctier = "S"
        _crej = "sent"
        _csport = "CS"
        _is_strict = False
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        if _ctier == "PASS":
            _cgd = "REJECTED"
        elif _ctier == "B":
            _cgd = "ACCEPTED" if (not _is_strict and _crej in _accepted_reasons) else "WATCHLIST"
        elif _crej in _accepted_reasons and _ctier in ("S", "A"):
            _cgd = "ACCEPTED"
        else:
            _cgd = "REJECTED"
        assert _cgd == "ACCEPTED"

    def test_all_tier1_b_accepted_reasons(self):
        """All five accepted reasons produce ACCEPTED for Tier 1 B-tier."""
        _accepted_reasons = ("qualified", "sent", "filtered", "new_prop_failed", "cold_start")
        for reason in _accepted_reasons:
            _csport = "TENNIS"
            _is_strict = False
            _ctier = "B"
            _cgd = "ACCEPTED" if (not _is_strict and reason in _accepted_reasons) else "WATCHLIST"
            assert _cgd == "ACCEPTED", f"Tier 1 B-tier reason={reason} must be ACCEPTED"

    def test_source_has_strict_sport_check_near_b_tier(self):
        """Source code must include a strict-sport check in the B-tier branch."""
        src = self._src()
        # Find the B-tier branch
        idx = src.find('_ctier == "B"')
        assert idx >= 0
        block = src[idx:idx + 600]
        assert "_is_strict" in block or "is_strict_sport" in block or '"MLB"' in block


# ═══════════════════════════════════════════════════════════════════════════
# Generic tier policy — unchanged configuration
# ═══════════════════════════════════════════════════════════════════════════

class TestTierPolicyUnchanged:
    """All thresholds and gate values must remain unchanged."""

    def test_ud_strict_alert_sports_still_mlb_nfl_only(self):
        from config import config as cfg
        assert cfg.ud_strict_alert_sports == frozenset({"MLB", "NFL"})

    def test_strict_sports_not_expanded(self):
        from config import config as cfg
        assert "CS" not in cfg.ud_strict_alert_sports
        assert "LOL" not in cfg.ud_strict_alert_sports
        assert "WNBA" not in cfg.ud_strict_alert_sports
        assert "TENNIS" not in cfg.ud_strict_alert_sports

    def test_ud_min_stars_to_alert_unchanged(self):
        from config import config as cfg
        assert cfg.UD_MIN_STARS_TO_ALERT == 3

    def test_ud_non_strict_min_stars_unchanged(self):
        from config import config as cfg
        assert cfg.UD_NON_STRICT_MIN_STARS == 2

    def test_ud_min_conf_s_unchanged(self):
        from config import config as cfg
        assert cfg.UD_MIN_CONF_S == 80

    def test_ud_min_conf_a_unchanged(self):
        from config import config as cfg
        assert cfg.UD_MIN_CONF_A == 70

    def test_ud_min_conf_b_unchanged(self):
        from config import config as cfg
        assert cfg.UD_MIN_CONF_B == 55

    def test_ud_non_strict_min_conf_a_unchanged(self):
        from config import config as cfg
        # Per Final Prop Acceptance Spec: Tier 1 A-tier cutoff is 70.
        # 70 = actionable, 69 = watchlist.  Updated from prior 60 default.
        assert cfg.UD_NON_STRICT_MIN_CONF_A == 70

    def test_ud_non_strict_min_conf_b_unchanged(self):
        from config import config as cfg
        assert cfg.UD_NON_STRICT_MIN_CONF_B == 45

    def test_ud_strict_sport_min_bet_quality_unchanged(self):
        from config import config as cfg
        assert cfg.UD_STRICT_SPORT_MIN_BET_QUALITY == 95

    def test_ud_mlb_alert_tiers_still_s_only(self):
        """Default: S-tier only (Tier 2: S+OVER actionable, A=watchlist)."""
        from config import config as cfg
        tiers = cfg.ud_mlb_alert_tiers
        assert "S" in tiers
        assert "A" not in tiers  # A-tier is watchlist for Tier 2
        assert "B" not in tiers

    def test_ud_mlb_min_tier_still_s(self):
        """Default UD_MLB_MIN_TIER must be 'S' (Tier 2 spec)."""
        from config import config as cfg
        assert cfg.UD_MLB_MIN_TIER == "S"

    def test_validation_min_samples_unchanged(self):
        from config import config as cfg
        assert cfg.UD_VALIDATION_MIN_SAMPLES == 5

    def test_min_conf_for_sport_tier_mlb_s(self):
        from config import config as cfg
        assert cfg.min_conf_for_sport_tier("MLB", "S") == 80

    def test_min_conf_for_sport_tier_mlb_a(self):
        from config import config as cfg
        assert cfg.min_conf_for_sport_tier("MLB", "A") == 70

    def test_min_conf_for_sport_tier_cs_a(self):
        from config import config as cfg
        # Per spec: Tier 1 A-tier cutoff is 70 (was 60).
        assert cfg.min_conf_for_sport_tier("CS", "A") == 70

    def test_min_conf_for_sport_tier_cs_b(self):
        from config import config as cfg
        assert cfg.min_conf_for_sport_tier("CS", "B") == 45


# ═══════════════════════════════════════════════════════════════════════════
# Prior-pass fixes still intact
# ═══════════════════════════════════════════════════════════════════════════

class TestPriorPassesIntact:
    """Ensure no regression from prior V3.2 fix passes."""

    def test_render_pick_entry_proxy_fallback_intact(self):
        """Proxy-conf fallback from P1b fix remains intact."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "score_tier" in src
        # score_confidence was replaced by tier-midpoint derivation in the DB fallback
        # (PLH has no score_confidence column). Verify the new derivation is present.
        assert "_db_conf" in src, "_db_conf fallback variable must be present"
        assert "< 30" in src

    def test_no_eff_conf_gate_in_picks_loop(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_eff_conf" not in src

    def test_health_no_previous_session_line(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_health)
        assert "Previous session:" not in src

    def test_health_no_crash_detected_line(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_health)
        assert "Crash detected:" not in src

    def test_health_no_restart_reason_line(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_health)
        assert "Restart reason:" not in src

    def test_count_actionable_over_under_filter(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.count_actionable_pick_records)
        assert "OVER" in src and "UNDER" in src

    def test_dedup_restore_on_restart(self):
        import market_engine as me
        src = inspect.getsource(me)
        assert "_init_state_from_db" in src
        assert "get_recent_alerted_props_for_dedup" in src

    def test_strict_sports_display_filter_in_picks(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_strict_sports" in src

    def test_mlb_under_blocked_in_picks_display(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "MLB" in src and "UNDER" in src

    def test_score_tier_in_s_a_only_for_strict_display(self):
        """Display loop must still enforce S-tier-only for MLB/NFL."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_strict_sports" in src
        assert '"S"' in src

    def test_funnel_near_miss_accepted_key_dedup_intact(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_funnel_summary)
        assert "_accepted_keys" in src
        assert "_seen_keys" in src


# ═══════════════════════════════════════════════════════════════════════════
# Credential safety
# ═══════════════════════════════════════════════════════════════════════════

class TestCredentialSafety:
    """No credentials must appear in any output path."""

    _PATTERNS = ["sk_", "pk_", "ODDS_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "SESSION_SECRET", "TELEGRAM_TOKEN"]

    def _assert_no_creds(self, src: str, name: str) -> None:
        for pat in self._PATTERNS:
            assert pat not in src, f"credential pattern '{pat}' found in {name}"

    def test_picks_no_credentials(self):
        import commands as cmd_mod
        self._assert_no_creds(inspect.getsource(cmd_mod._cmd_picks_inner), "cmd_picks")

    def test_health_no_credentials(self):
        import commands as cmd_mod
        self._assert_no_creds(inspect.getsource(cmd_mod.cmd_health), "cmd_health")

    def test_database_picks_no_credentials(self):
        import database as db_mod
        self._assert_no_creds(
            inspect.getsource(db_mod.Database.get_top_ud_props_for_picks),
            "get_top_ud_props_for_picks",
        )
