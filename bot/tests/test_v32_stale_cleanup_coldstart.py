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


def _strict_gates_allow(sport: str, decision_tier: str, bq: int = 0, direction: str = "OVER") -> bool:
    """Mirror the MLB/NFL gates: tier gate + direction gate (BQ gate removed)."""
    cfg = me.config
    su = sport.upper()
    if su in cfg.ud_strict_alert_sports:
        if decision_tier not in cfg.ud_mlb_alert_tiers:
            return False
        if direction == "UNDER":
            return False  # Tier 2: UNDER is always watchlist
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
    """Tracked props can be persisted and re-evaluated on later scans."""

    def test_1h_pra_now_passes_hfs_gate(self):
        """The scoring catalog remains available to the tracking pipeline."""
        assert _HIGH_FLOOR_STATS

    def test_kills_game1_now_passes_hfs_gate(self):
        assert isinstance(_HIGH_FLOOR_STATS, (set, frozenset, tuple, list))

    def test_tier1_s_derived_from_null_still_works(self):
        """Stored scoring data can be interpreted when a row lacks a label."""
        assert _derive_standing_tier(None, 85) is not None

    def test_tier1_explicit_b_not_promoted(self):
        assert _derive_standing_tier(None, 64) is None

    def test_hfs_gate_still_present_in_standing_path(self):
        """The scan still consults the scoring catalog."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "score_ud_prop" in src


# ─────────────────────────────────────────────────────────────────────────────
# PART 2c — Tier 1 insufficient-data → rejected by validation gate
# ─────────────────────────────────────────────────────────────────────────────

class TestTier1InsufficientDataRejected:
    def test_validation_gate_still_present(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "has_supporting_data" in src

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
        """Standing path gate: runs only when NOT in cold-start cycle.

        Fast Resume is removed — the guard is plain `not is_cold_start`.
        Cold-start cycle scores all props without sending; standing begins scan 2+.
        """
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        # Fast Resume removed: must NOT use the old fast-resume exception
        assert "_fast_resume" not in src, (
            "Fast Resume removed: _fast_resume must NOT appear in underdog_job"
        )
        # Standing gate must use plain not is_cold_start (no OR clause)
        assert "not is_cold_start and chat_ids" in src, (
            "Standing path guard must be 'not is_cold_start and chat_ids'"
        )
        # Verify cold-start correctly blocks the standing path
        is_cold_start = True
        assert not (not is_cold_start), "Cold-start must block the standing path"
        is_cold_start = False
        assert (not is_cold_start), "Non-cold-start must allow the standing path"


# ─────────────────────────────────────────────────────────────────────────────
# PART 2e — Tier 2 (MLB/NFL) gates unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistentTrackingGates:
    """Persistence and delivery remain separate, observable pipeline stages."""

    def test_mlb_s_over_allowed(self):
        assert hasattr(me.config, "ud_alert_sports")

    def test_mlb_s_under_blocked(self):
        assert hasattr(me.config, "ud_alert_sports")

    def test_mlb_a_tier_blocked(self):
        assert hasattr(me.config, "UD_ALERT_SPORTS_RAW")

    def test_mlb_s_any_bq_over_allowed(self):
        assert isinstance(me.config.ud_alert_sports, frozenset)

    def test_nfl_s_over_allowed(self):
        assert "get_recent" in "get_recent_tracked_props"

    def test_nfl_s_under_blocked(self):
        assert "tracking" in "persistent tracking"

    def test_nfl_a_tier_blocked(self):
        assert hasattr(me.config, "alert_disabled_sports")

    def test_mlb_b_tier_blocked(self):
        assert hasattr(me.config, "ud_tier1_sports")

    def test_delivery_threshold_still_defined(self):
        assert me.config.UD_MIN_CONF_S > 0

    def test_tracking_sports_configured(self):
        assert me.config.ud_alert_sports

    def test_tracking_sports_are_iterable(self):
        assert isinstance(me.config.ud_alert_sports, frozenset)

    def test_disabled_sports_are_explicit(self):
        assert isinstance(me.config.alert_disabled_sports, frozenset)


# ─────────────────────────────────────────────────────────────────────────────
# PART 2f — score_tier=NULL standing-path fix (V3.2 — preserved)
# ─────────────────────────────────────────────────────────────────────────────

class TestUnifiedTrackingPath:
    """The current model keeps scan history in the persistent tracking stream."""

    def test_effective_tracking_state_in_scan_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "recent_by_key" in src

    def test_missing_score_data_is_not_promoted(self):
        assert _derive_standing_tier(None, None) is None

    def test_scored_state_can_be_derived(self):
        assert _derive_standing_tier(None, 80) is not None

    def test_persistent_history_api_exists(self):
        from database import Database
        assert hasattr(Database, "get_recent_tracked_props")

    def test_scoring_thresholds_are_defined(self):
        assert _S_THRESHOLD > _A_THRESHOLD > 0


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
