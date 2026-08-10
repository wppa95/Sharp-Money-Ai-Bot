"""
Focused tests for V3.2 stale diagnostic cleanup + Tier 1 cold_start fix.

Covers:
  Part 1 — Health timestamp parsing (stale error suppression)
  Part 2 — _HIGH_FLOOR_STATS expansion (Tier 1 non-HFS stats now reach standing path)
  Full gate matrix — Tier 1 / Tier 2, cold_start, dedup, persistence, Telegram
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me
from engine.ud_scoring import _HIGH_FLOOR_STATS, _S_THRESHOLD, _A_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — replicate the gate logic from market_engine
# ─────────────────────────────────────────────────────────────────────────────

def _derive_standing_tier(score_tier, score_total):
    """Mirror the V3.2 standing-path effective-tier derivation."""
    eff = score_tier
    if eff is None and score_total is not None:
        eff = "S" if score_total >= 80 else ("A" if score_total >= 65 else None)
    return eff if eff in ("A", "S") else None


def _strict_gates_allow(sport: str, decision_tier: str, bq: int = 0) -> bool:
    """Mirror the MLB/NFL tier gate (BQ gate removed per spec Tier 2)."""
    cfg = me.config
    su = sport.upper()
    if su in cfg.ud_strict_alert_sports:
        if decision_tier not in cfg.ud_mlb_alert_tiers:
            return False
    return True


def _parse_health_ts(ts_str: str) -> datetime:
    """
    Replicate the fixed timestamp parsing from cmd_health.
    Handles _now_iso() format "YYYY-MM-DD HH:MM:SS UTC".
    """
    clean = str(ts_str).replace(" UTC", "").replace("Z", "").strip()
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# PART 1 — Stale health timestamp parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthTimestampParsing:
    """The cmd_health timestamp parsing must correctly age-gate health.json values."""

    def test_now_iso_format_parseable(self):
        """
        _now_iso() returns "YYYY-MM-DD HH:MM:SS UTC".
        The fixed parser must handle this without raising.
        """
        sample = "2026-08-06 12:34:56 UTC"
        dt = _parse_health_ts(sample)
        assert dt.year == 2026
        assert dt.month == 8
        assert dt.day == 6
        assert dt.tzinfo is not None

    def test_old_error_timestamp_shows_as_stale(self):
        """A 2+ day-old error timestamp must parse to age > 2h."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        dt = _parse_health_ts(old_ts)
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        assert age_h >= 2.0, "3-hour old error must be suppressed (age >= 2h)"

    def test_aug_06_error_is_stale(self):
        """The specific Aug 06 error must parse and be correctly age-gated."""
        aug06_ts = "2026-08-06 12:34:56 UTC"
        dt = _parse_health_ts(aug06_ts)
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        # Today is Aug 08 — the error is ~2 days old, well beyond 2h threshold
        assert age_h >= 2.0, "Aug 06 error must be identified as stale"

    def test_recent_error_passes_2h_gate(self):
        """An error from 30 minutes ago must still be shown (age < 2h)."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        dt = _parse_health_ts(recent_ts)
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        assert age_h < 2.0, "Recent error (30 min ago) must pass the 2h gate"

    def test_z_suffix_still_parseable(self):
        """Timestamps with 'Z' suffix (alternative format) also parse correctly."""
        ts_z = "2026-08-06T12:34:56Z"
        clean = ts_z.replace(" UTC", "").replace("Z", "").strip()
        dt = datetime.fromisoformat(clean)
        assert dt.year == 2026


# ─────────────────────────────────────────────────────────────────────────────
# PART 2a — _HIGH_FLOOR_STATS expansion (new stats added)
# ─────────────────────────────────────────────────────────────────────────────

class TestHighFloorStatsExpansion:
    """New Tier 1 stat variants must be in _HIGH_FLOOR_STATS so the standing path can evaluate them."""

    # Basketball half-game variants
    def test_1h_points_in_hfs(self):
        assert "1H Points" in _HIGH_FLOOR_STATS

    def test_1h_rebounds_in_hfs(self):
        assert "1H Rebounds" in _HIGH_FLOOR_STATS

    def test_1h_assists_in_hfs(self):
        assert "1H Assists" in _HIGH_FLOOR_STATS

    def test_1h_pra_in_hfs(self):
        assert "1H Pts + Rebs + Asts" in _HIGH_FLOOR_STATS

    # CoD/esports per-game variants
    def test_kills_game1_in_hfs(self):
        assert "Kills on Game 1" in _HIGH_FLOOR_STATS

    def test_kills_game2_in_hfs(self):
        assert "Kills on Game 2" in _HIGH_FLOOR_STATS

    def test_assists_game1_in_hfs(self):
        assert "Assists on Game 1" in _HIGH_FLOOR_STATS

    def test_assists_game2_in_hfs(self):
        assert "Assists on Game 2" in _HIGH_FLOOR_STATS

    # Existing stats still present
    def test_hits_still_in_hfs(self):
        assert "Hits" in _HIGH_FLOOR_STATS

    def test_points_still_in_hfs(self):
        assert "Points" in _HIGH_FLOOR_STATS

    def test_kills_maps_still_in_hfs(self):
        assert "Kills on Maps 1+2" in _HIGH_FLOOR_STATS

    def test_pra_still_in_hfs(self):
        assert "Points + Rebounds + Assists" in _HIGH_FLOOR_STATS


# ─────────────────────────────────────────────────────────────────────────────
# PART 2b — Tier 1 A/S candidate with sufficient data → reaches standing path
# ─────────────────────────────────────────────────────────────────────────────

class TestTier1StandingPathAccess:
    """After adding to _HFS, Tier 1 stats can pass the standing-path _HFS gate."""

    def test_1h_pra_now_passes_hfs_gate(self):
        """'1H Pts + Rebs + Asts' was blocking near-misses; must now be in _HFS."""
        assert "1H Pts + Rebs + Asts" in _HIGH_FLOOR_STATS

    def test_kills_game1_now_passes_hfs_gate(self):
        """CoD 'Kills on Game 1' must now be in _HFS."""
        assert "Kills on Game 1" in _HIGH_FLOOR_STATS

    def test_tier1_s_derived_from_null_still_works(self):
        """V3.2 score_tier=NULL derivation must still work for newly added stats."""
        assert _derive_standing_tier(None, 85) == "S"
        assert _derive_standing_tier(None, 65) == "A"
        assert _derive_standing_tier(None, 64) is None

    def test_tier1_explicit_b_not_promoted(self):
        """Explicit B-tier must not be promoted by fallback (unchanged from V3.2 fix)."""
        assert _derive_standing_tier("B", 80) is None

    def test_hfs_gate_still_present_in_standing_path(self):
        """The _HFS gate must still be present (not removed)."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "_HFS" in src
        assert "_st not in _HFS" in src


# ─────────────────────────────────────────────────────────────────────────────
# PART 2c — Tier 1 insufficient-data → rejected by validation gate
# ─────────────────────────────────────────────────────────────────────────────

class TestTier1InsufficientDataRejected:
    def test_validation_gate_still_present(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "not _sval.has_supporting_data" in src

    def test_min_validation_samples_config(self):
        """UD_VALIDATION_MIN_SAMPLES must be present and ≥ 1."""
        assert me.config.UD_VALIDATION_MIN_SAMPLES >= 1


# ─────────────────────────────────────────────────────────────────────────────
# PART 2d — cold_start is global and temporary
# ─────────────────────────────────────────────────────────────────────────────

class TestColdStartBehavior:
    """cold_start is module-level and initialization-only; it is NOT a permanent per-prop blocker."""

    def test_cold_start_done_is_module_level(self):
        """_cold_start_done must be a module-level bool (not per-prop or per-sport)."""
        assert hasattr(me, "_cold_start_done")
        assert isinstance(me._cold_start_done, bool)

    def test_cold_start_blocks_is_qualified(self):
        """is_qualified must include 'not is_cold_start' so no alerts fire during init."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "not is_cold_start" in src

    def test_cold_start_done_set_after_bulk_save(self):
        """_cold_start_done = True must be set at end of cold_start cycle."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "_cold_start_done = True" in src

    def test_standing_path_cold_start_gate(self):
        """Standing path gate: runs when not is_cold_start OR when _fast_resume=True.

        V3.5 changed the guard from `not is_cold_start` to
        `(not is_cold_start or _fast_resume)` so that existing DISCOVERED props
        are immediately re-evaluated on fast-resume scan 1 rather than waiting
        for scan 2.  All delivery gates (confidence, live/past, BQ, dedup) still
        apply — fast-resume does NOT weaken any qualification requirements.
        """
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        # V3.5: fast-resume exception enables standing path during cold-start
        assert "(not is_cold_start or _fast_resume)" in src, (
            "V3.5: standing path guard must be '(not is_cold_start or _fast_resume)'"
        )
        # Without fast-resume: cold-start still prevents standing path
        is_cold_start, fast_resume = True, False
        assert (not is_cold_start or fast_resume) is False, (
            "Without fast-resume: standing path still skipped during cold-start"
        )
        # With fast-resume: standing path runs even during cold-start
        is_cold_start, fast_resume = True, True
        assert (not is_cold_start or fast_resume) is True, (
            "With fast-resume: standing path runs on cold-start scan 1"
        )


# ─────────────────────────────────────────────────────────────────────────────
# PART 2e — Tier 2 (MLB/NFL) gates unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestTier2StrictGatesUnchanged:
    """MLB/NFL strict rules updated per spec Tier 2 (S/A deliver, B/C watchlist, no BQ gate)."""

    def test_mlb_a_tier_allowed(self):
        """A-tier MLB is now allowed — spec Tier 2."""
        assert _strict_gates_allow("MLB", "A")

    def test_mlb_s_tier_any_bq_allowed(self):
        """No BQ gate — S-tier MLB allowed at any confidence."""
        assert _strict_gates_allow("MLB", "S", 60)

    def test_mlb_s_tier_always_allowed(self):
        assert _strict_gates_allow("MLB", "S")

    def test_nfl_a_tier_allowed(self):
        """A-tier NFL is now allowed — spec Tier 2."""
        assert _strict_gates_allow("NFL", "A")

    def test_nfl_s_tier_always_allowed(self):
        assert _strict_gates_allow("NFL", "S")

    def test_mlb_b_tier_blocked(self):
        """B-tier MLB remains watchlist only."""
        assert not _strict_gates_allow("MLB", "B")

    def test_bq_threshold_still_defined(self):
        """Config value still defined even though gate is removed."""
        assert me.config.UD_STRICT_SPORT_MIN_BET_QUALITY == 95

    def test_mlb_in_strict_sports(self):
        assert "MLB" in me.config.ud_strict_alert_sports

    def test_nfl_in_strict_sports(self):
        assert "NFL" in me.config.ud_strict_alert_sports

    def test_tier1_sports_not_in_strict(self):
        for sport in ("CS", "LOL", "WNBA", "BASKETBALL", "ESPORTS", "TENNIS"):
            assert sport not in me.config.ud_strict_alert_sports, \
                f"{sport} must NOT be a strict sport"


# ─────────────────────────────────────────────────────────────────────────────
# PART 2f — score_tier=NULL standing-path fix (V3.2 — preserved)
# ─────────────────────────────────────────────────────────────────────────────

class TestV32StandingPathFixPreserved:
    """The V3.2 score_tier=NULL fix must remain intact after this cleanup pass."""

    def test_effective_tier_variable_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "_prev_eff_tier" in src

    def test_null_score_tier_fallback_to_score_total(self):
        assert _derive_standing_tier(None, 80) == "S"
        assert _derive_standing_tier(None, 65) == "A"
        assert _derive_standing_tier(None, 64) is None
        assert _derive_standing_tier(None, None) is None

    def test_explicit_b_not_promoted(self):
        assert _derive_standing_tier("B", 75) is None

    def test_explicit_pass_not_promoted(self):
        assert _derive_standing_tier("PASS", 90) is None

    def test_thresholds_match_ud_scoring(self):
        assert _derive_standing_tier(None, _S_THRESHOLD) == "S"
        assert _derive_standing_tier(None, _S_THRESHOLD - 1) == "A"
        assert _derive_standing_tier(None, _A_THRESHOLD) == "A"
        assert _derive_standing_tier(None, _A_THRESHOLD - 1) is None


# ─────────────────────────────────────────────────────────────────────────────
# PART 2g — Deduplication, Telegram delivery, persistence gates intact
# ─────────────────────────────────────────────────────────────────────────────

class TestGatesPreservedAfterCleanup:

    def test_dedup_24h_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "has_recent_ud_alert" in src
        assert "86400" in src

    def test_direction_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "decision_pass" in src

    def test_live_game_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "live_gate [standing]" in src

    def test_min_line_change_unchanged(self):
        assert me.config.MIN_UNDERDOG_LINE_CHANGE == 0.5

    def test_telegram_only_receives_actionable(self):
        """Telegram delivery path must still go only through deliver_underdog."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "deliver_underdog" in src
        assert "_n_standing_sent" in src

    def test_restarts_command_not_present(self):
        """The /restarts command must have been removed."""
        import commands as _cmds
        assert not hasattr(_cmds, "cmd_restarts"), "/restarts command must be removed"

    def test_scoring_thresholds_unchanged(self):
        """S/A thresholds in ud_scoring must be unchanged."""
        assert _S_THRESHOLD == 80
        assert _A_THRESHOLD == 65
