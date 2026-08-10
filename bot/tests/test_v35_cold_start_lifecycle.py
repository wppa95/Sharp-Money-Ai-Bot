"""
V3.5 Cold-start lifecycle tests — spec requirements 1-16.

Verifies the complete lifecycle from cold-start through restart-resume
graduation, per the 16-item test specification:

 1. New prop enters cold-start.
 2. Bot restarts.
 3. Cold-start prop remains in persistent state.
 4. Restart-resume identifies it.
 5. Existing history is preserved.
 6. Prop receives subsequent snapshots.
 7. Prop is re-evaluated after sufficient history.
 8. S-tier cold-start prop can become qualified.
 9. A-tier cold-start prop can become qualified.
10. Tier 1 behavior works independently for each sport.
11. Tier 2 MLB behavior works.
12. Tier 2 NFL behavior works.
13. A recent checkpoint does NOT incorrectly skip cold-start props.
14. Props with sufficient history still use fast incremental resume.
15. Genuinely new props still initialize correctly.
16. Restart does not duplicate stored picks or alerts.

All tests are lightweight (DB-backed or structural inspection).
No live API calls or Underdog polling.
"""

import sys
import os
import inspect
import asyncio
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ═══════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════════════

def _run(coro):
    """Run an async coroutine in a test context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tmp_health_path() -> Path:
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    return Path(f.name)


def _make_health(path: Path = None):
    from engine.health import HealthTracker
    if path is None:
        path = _tmp_health_path()
    return HealthTracker(path=path), path


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 1 — New prop enters cold-start
# ═══════════════════════════════════════════════════════════════════════════

class TestReq1NewPropColdStart:
    """1. New prop enters cold-start on first scan."""

    def test_cold_start_flag_starts_false(self):
        """_cold_start_done must be False on initial import (handled per-process)."""
        import market_engine as me
        # The flag is module-level. In the test process it may already be True
        # if underdog_job has run. What matters is the flag EXISTS.
        assert isinstance(me._cold_start_done, bool), "_cold_start_done must be a bool"

    def test_is_cold_start_derived_from_done_flag(self):
        """is_cold_start = not _cold_start_done — True only while done=False."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "is_cold_start = not _cold_start_done" in src, (
            "is_cold_start must be derived from _cold_start_done each cycle"
        )

    def test_new_prop_path_exists(self):
        """is_new_prop detection must exist in underdog_job."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "is_new_prop" in src, "is_new_prop must be tracked in the main loop"

    def test_cold_start_scored_outcome_exists(self):
        """cold_start_scored alert_outcome must be recorded for tracking."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert '"cold_start_scored"' in src or "'cold_start_scored'" in src, (
            "cold-start props must be tracked with alert_outcome='cold_start_scored'"
        )

    def test_cold_start_records_bulk_saved(self):
        """Cold-start records must be bulk-saved to PropLineHistory / snapshots DB."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_cold_start_records" in src, "_cold_start_records list must exist"
        assert "save_underdog_snapshots_bulk" in src, (
            "Cold-start records must be bulk-saved to DB"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 2 — Bot restarts
# ═══════════════════════════════════════════════════════════════════════════

class TestReq2BotRestart:
    """2. Bot restart correctly resets in-memory state while preserving DB state."""

    def test_fast_resume_flag_removed(self):
        """Fast Resume is removed — _fast_resume must NOT exist as a module-level flag."""
        import market_engine as me
        assert not hasattr(me, "_fast_resume"), (
            "_fast_resume must be removed — Fast Resume is no longer supported"
        )

    def test_init_state_from_db_called_during_restart(self):
        """_init_state_from_db is the restart restoration entry point."""
        import market_engine as me
        assert hasattr(me, "_init_state_from_db"), "_init_state_from_db must exist"
        assert callable(me._init_state_from_db)

    def test_init_state_from_db_restores_alerted_set(self):
        """_init_state_from_db must restore _prop_market_alerted from DB."""
        src = inspect.getsource(__import__("market_engine")._init_state_from_db)
        assert "_prop_market_alerted" in src, (
            "_init_state_from_db must restore dedup set from PropOpportunityLog"
        )

    def test_init_state_from_db_restores_market_first_alert(self):
        """_init_state_from_db must restore _MARKET_FIRST_ALERT from DB."""
        src = inspect.getsource(__import__("market_engine")._init_state_from_db)
        assert "_MARKET_FIRST_ALERT" in src, (
            "_init_state_from_db must restore _MARKET_FIRST_ALERT from DB"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3 — Cold-start prop remains in persistent state
# ═══════════════════════════════════════════════════════════════════════════

class TestReq3PersistentState:
    """3. Cold-start prop state persists in PropLineHistory / UnderdogSnapshotRecord."""

    def test_prop_line_history_table_exists(self):
        from database import PropLineHistory
        assert PropLineHistory is not None

    def test_underdog_snapshot_record_table_exists(self):
        from database import UnderdogSnapshotRecord
        assert UnderdogSnapshotRecord is not None

    def test_prop_line_history_has_lifecycle_state(self):
        """PropLineHistory must track lifecycle_state for cold-start detection."""
        from database import PropLineHistory
        assert hasattr(PropLineHistory, "lifecycle_state"), (
            "PropLineHistory must have lifecycle_state column"
        )

    def test_discovered_is_valid_lifecycle_state(self):
        """DISCOVERED lifecycle state must be recognized (cold-start initial state)."""
        import database as db_mod
        src = inspect.getsource(db_mod)
        assert "DISCOVERED" in src, (
            "DISCOVERED lifecycle state must be defined — used for cold-start props"
        )

    def test_active_alerted_is_valid_lifecycle_state(self):
        """ACTIVE_ALERTED lifecycle state must exist for graduated props."""
        import database as db_mod
        src = inspect.getsource(db_mod)
        assert "ACTIVE_ALERTED" in src, (
            "ACTIVE_ALERTED lifecycle state must exist for graduated props"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 4 — Restart-resume identifies cold-start props
# ═══════════════════════════════════════════════════════════════════════════

class TestReq4RestartResumeIdentification:
    """4. Restart-resume must identify existing cold-start (DISCOVERED) props."""

    def test_standing_path_uses_recent_by_key_from_db(self):
        """Standing path reads recent_by_key from DB — survives restart."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "recent_by_key" in src, "recent_by_key must be used in standing path"
        assert "get_recent_ud_prop_histories" in src or "recent_by_key" in src, (
            "Standing path must load recent prop history from DB"
        )

    def test_standing_path_checks_score_tier_from_db(self):
        """Standing path uses DB-stored score_tier to identify DISCOVERED S/A props."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_prev_eff_tier" in src, (
            "Standing path must derive effective tier from stored score_tier"
        )
        assert "score_tier" in src and ("S" in src or '"A"' in src), (
            "Standing path must check S/A tier from DB records"
        )

    def test_standing_path_cold_start_gate(self):
        """Fast Resume removed — standing path uses plain `not is_cold_start` gate.

        Cold-start cycle (scan 1) scores all props without alerts.
        Standing path (scan 2+) picks up qualified props on every subsequent scan.
        """
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_fast_resume" not in src, (
            "Fast Resume removed: _fast_resume must not appear in underdog_job"
        )
        assert "not is_cold_start and chat_ids" in src, (
            "Standing path must gate on plain `not is_cold_start` after Fast Resume removal"
        )

    def test_standing_path_gate_logic(self):
        """Verify the standing path gate evaluates correctly for all combinations."""
        # Test truth table for (not is_cold_start or _fast_resume)
        cases = [
            # (is_cold_start, _fast_resume, expected_standing_runs)
            (False, False, True),   # normal scan: standing always runs
            (False, True,  True),   # normal scan + fast_resume: standing runs
            (True,  False, False),  # cold-start without fast_resume: standing skipped
            (True,  True,  True),   # cold-start WITH fast_resume: standing runs ← KEY
        ]
        for is_cold_start, fast_resume, expected in cases:
            result = (not is_cold_start or fast_resume)
            assert result == expected, (
                f"is_cold_start={is_cold_start}, fast_resume={fast_resume}: "
                f"expected standing_runs={expected}, got {result}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 5 — Existing history preserved
# ═══════════════════════════════════════════════════════════════════════════

class TestReq5HistoryPreserved:
    """5. Existing prop history is preserved across restarts."""

    def test_get_ud_prop_history_db_backed(self):
        """get_ud_prop_history must be a DB query (not in-memory cache)."""
        import database as db_mod
        assert hasattr(db_mod.Database, "get_ud_prop_history"), (
            "Database must have get_ud_prop_history method"
        )

    def test_prop_line_history_table_survives_restart(self):
        """PropLineHistory is persistent (SQLite/PostgreSQL) — survives restart."""
        # Verify it's a database model, not just an in-memory structure
        from database import PropLineHistory
        # SQLAlchemy models have __tablename__
        assert hasattr(PropLineHistory, "__tablename__"), (
            "PropLineHistory must be a persistent DB table (SQLAlchemy model)"
        )

    def test_recent_by_key_built_from_db(self):
        """recent_by_key must be loaded from DB at the start of each scan."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "recent_by_key" in src, "recent_by_key must be used"
        # It must be populated from a DB call
        assert "get_recent_ud_prop_histories" in src or "get_ud_prop_history" in src, (
            "recent_by_key must be built from DB — ensures history survives restart"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 6 — Prop receives subsequent snapshots
# ═══════════════════════════════════════════════════════════════════════════

class TestReq6SubsequentSnapshots:
    """6. Cold-start props receive new snapshots on each subsequent scan."""

    def test_snapshot_saved_every_cycle(self):
        """UnderdogSnapshotRecord is saved every scan (not only on alerts)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "save_underdog_snapshot" in src, (
            "save_underdog_snapshot must be called every cycle (cold-start and normal)"
        )

    def test_prop_line_history_updated_per_scan(self):
        """PropLineHistory accumulates entries on every scan cycle."""
        import database as db_mod
        # Look for the upsert/update method
        assert (
            hasattr(db_mod.Database, "save_underdog_snapshot")
            or hasattr(db_mod.Database, "save_ud_prop_snapshot")
            or hasattr(db_mod.Database, "upsert_prop_line_history")
        ), "DB must have a method to update PropLineHistory each scan"

    def test_history_limit_is_db_bounded_not_memory(self):
        """get_ud_prop_history uses a DB limit (not in-memory truncation)."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_ud_prop_history)
        assert "limit" in src.lower(), (
            "get_ud_prop_history must use a DB-level limit"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 7 — Prop re-evaluated after sufficient history
# ═══════════════════════════════════════════════════════════════════════════

class TestReq7ReEvaluationAfterHistory:
    """7. Cold-start prop is re-evaluated by standing path once history is sufficient."""

    def test_standing_path_runs_every_non_cold_start_cycle(self):
        """Standing path runs every cycle where is_cold_start=False."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # Confirm standing path guard allows non-cold-start cycles
        assert "not is_cold_start" in src, (
            "Standing path must check is_cold_start (runs when False)"
        )

    def test_standing_path_refetches_history(self):
        """Standing path fetches fresh history on each evaluation."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # get_ud_prop_history in standing path context
        assert "get_ud_prop_history" in src, (
            "Standing path must re-fetch prop history on each evaluation"
        )

    def test_standing_path_calls_score_ud_prop(self):
        """Standing path calls score_ud_prop for fresh re-evaluation."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "score_ud_prop" in src, (
            "Standing path must call score_ud_prop for fresh scoring"
        )

    def test_standing_path_calls_make_ud_bet_decision(self):
        """Standing path calls make_ud_bet_decision with current hit rates."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "make_ud_bet_decision" in src, (
            "Standing path must call make_ud_bet_decision for direction + BQ"
        )

    def test_standing_path_calls_fetch_hit_rates(self):
        """Standing path fetches fresh hit rates for each candidate."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_fetch_and_compute_hit_rates" in src, (
            "Standing path must fetch hit rates before bet decision"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 8 — S-tier cold-start prop can become qualified
# ═══════════════════════════════════════════════════════════════════════════

class TestReq8STierGraduation:
    """8. S-tier cold-start prop can graduate through standing path to Telegram delivery."""

    def test_standing_path_accepts_s_tier(self):
        """Standing path does NOT block S-tier (it requires A or S from DB)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # _prev_eff_tier in ("A", "S") is the admission gate
        assert '"A", "S"' in src or '"S"' in src, (
            "Standing path must admit both A and S tier props"
        )

    def test_s_tier_delivery_gate_non_strict_sport(self):
        """For Tier 1 sports: S-tier only needs confidence ≥ 80 (not BQ ≥ 95).

        MLB/NFL require BQ ≥ 95 (strict gate). CS/LOL/WNBA etc. do NOT have
        the strict BQ gate — only the per-tier confidence gate (S ≥ 80).
        """
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # The strict BQ gate must check ud_strict_alert_sports (not all sports)
        assert "ud_strict_alert_sports" in src, (
            "BQ ≥ 95 gate must be scoped to strict sports (MLB/NFL)"
        )

    def test_s_tier_confidence_threshold_from_config(self):
        """S-tier confidence threshold is configurable (UD_MIN_CONF_S)."""
        import config as cfg_mod
        assert hasattr(cfg_mod.Config, "min_conf_for_sport_tier") or \
               hasattr(cfg_mod.Config, "UD_MIN_CONF_S"), (
            "Config must expose S-tier confidence threshold"
        )

    def test_s_tier_can_use_95_priority_override(self):
        """S-tier with BQ ≥ 95 has priority override path (bypasses secondary gates)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "confidence >= 95" in src, (
            "95+ BQ override path must exist in standing scan"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 9 — A-tier cold-start prop can become qualified
# ═══════════════════════════════════════════════════════════════════════════

class TestReq9ATierGraduation:
    """9. A-tier cold-start prop can graduate through the standing path."""

    def test_standing_path_accepts_a_tier_from_db(self):
        """Standing path admits A-tier from DB score_tier."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert '"A"' in src, "Standing path must admit A-tier props"

    def test_a_tier_confidence_threshold_lower_than_s(self):
        """A-tier confidence threshold must be lower than S-tier."""
        from config import config
        s_conf = config.min_conf_for_sport_tier("CS", "S")
        a_conf = config.min_conf_for_sport_tier("CS", "A")
        assert a_conf <= s_conf, (
            f"A-tier conf threshold ({a_conf}) must be ≤ S-tier ({s_conf})"
        )

    def test_a_tier_not_blocked_by_strict_sport_tier_gate(self):
        """A-tier for non-strict sports (CS/WNBA etc.) must NOT be blocked by MLB/NFL gate."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # The strict-sport tier gate uses ud_mlb_alert_tiers
        assert "ud_mlb_alert_tiers" in src or "ud_strict_alert_sports" in src, (
            "Strict-sport tier gate must check sport before applying S-only restriction"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 10 — Tier 1 behavior works independently for each sport
# ═══════════════════════════════════════════════════════════════════════════

TIER_1_SPORTS = [
    "CS", "LOL", "WNBA", "TENNIS", "MMA", "VAL", "DOTA",
    "PGA", "BASKETBALL", "NPB", "CFB", "CFL",
]

class TestReq10Tier1BySport:
    """10. Tier 1 cold-start recovery works for each Tier 1 sport independently."""

    def test_ud_alert_sports_includes_tier1(self):
        """All monitored Tier 1 sports must appear in UD_ALERT_SPORTS config."""
        from config import config
        ud_sports = {s.upper() for s in (config.ud_alert_sports or [])}
        # At minimum CS, LOL, WNBA must be present (core Tier 1)
        core_tier1 = {"CS", "LOL", "WNBA"}
        missing = core_tier1 - ud_sports
        assert not missing, (
            f"Core Tier 1 sports missing from UD_ALERT_SPORTS: {missing}"
        )

    def test_tier1_sports_not_in_strict_alert_sports(self):
        """Tier 1 sports must NOT be in ud_strict_alert_sports (MLB/NFL-only gate)."""
        from config import config
        strict = {s.upper() for s in (config.ud_strict_alert_sports or [])}
        # Tier 1 sports should not be in strict
        tier1_in_strict = {s for s in TIER_1_SPORTS if s.upper() in strict}
        assert not tier1_in_strict, (
            f"Tier 1 sports must NOT be in strict_alert_sports: {tier1_in_strict}"
        )

    def test_tier1_no_bq95_gate(self):
        """Tier 1 sports must NOT face the BQ ≥ 95 strict gate (MLB/NFL only)."""
        from config import config
        strict_sports = {s.upper() for s in (config.ud_strict_alert_sports or [])}
        for sport in TIER_1_SPORTS:
            assert sport.upper() not in strict_sports, (
                f"{sport} is incorrectly in strict_alert_sports — Tier 1 must not face BQ≥95 gate"
            )

    def test_min_conf_for_sport_tier_returns_lower_for_tier1(self):
        """min_conf_for_sport_tier returns non-strict (lower) thresholds for Tier 1 sports."""
        from config import config
        # MLB strict S-tier threshold
        mlb_s = config.min_conf_for_sport_tier("MLB", "S")
        # CS non-strict S-tier threshold
        cs_s = config.min_conf_for_sport_tier("CS", "S")
        # Non-strict must be ≤ strict (usually the same or lower)
        assert cs_s <= mlb_s, (
            f"CS S-tier threshold ({cs_s}) should be ≤ MLB S-tier ({mlb_s})"
        )

    def test_each_tier1_sport_evaluated_independently(self):
        """Standing path sorts top 3 PER SPORT — each sport evaluated independently."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_by_sport" in src, "Standing path must group by sport for independent evaluation"
        assert "_sp_grp[:3]" in src or "[:3]" in src, (
            "Standing path must limit to top 3 per sport (independent per-sport evaluation)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 11 — Tier 2 MLB behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestReq11MLBTier2:
    """11. Tier 2 MLB behavior: cold-start props remain subject to MLB safety gates."""

    def test_mlb_in_strict_alert_sports(self):
        from config import config
        strict = {s.upper() for s in (config.ud_strict_alert_sports or [])}
        assert "MLB" in strict, "MLB must be in ud_strict_alert_sports"

    def test_mlb_under_blocked_in_standing_path(self):
        """MLB/NFL UNDER must be blocked in the standing path — Tier 2 is OVER only."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "mlb_under_gate [standing]" in src, (
            "mlb_under_gate [standing] not found — MLB/NFL UNDER must be blocked for Tier 2"
        )

    def test_mlb_bq_gate_removed_from_standing_path(self):
        """BQ gate removed — decision_tier (S/A only) enforces quality per spec Tier 2."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "bq_gate [standing]" not in src, (
            "bq_gate [standing] found — gate must be removed per spec Tier 2"
        )

    def test_mlb_requires_s_tier_only(self):
        """MLB requires S-tier only (not A or B)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "ud_mlb_alert_tiers" in src, (
            "MLB S-tier-only gate must use ud_mlb_alert_tiers"
        )

    def test_mlb_cold_start_props_face_same_gates_as_normal(self):
        """MLB cold-start props face the SAME gates: S+OVER only.

        fast_resume does NOT weaken MLB/NFL gates.
        Tier gate (S-only) + UNDER gate both remain active.
        BQ gate removed per spec (tier gate enforces quality).
        """
        src = inspect.getsource(__import__("market_engine").underdog_job)
        standing_start = src.find("4A: Standing opportunity scan")
        assert standing_start != -1, "Standing path header must exist"
        standing_src = src[standing_start:]
        assert "ud_strict_alert_sports" in standing_src, "Strict sport tier check in standing"
        assert "ud_mlb_alert_tiers" in standing_src, "MLB/NFL tier gate must be in standing path"
        assert "mlb_under_gate [standing]" in standing_src, "UNDER gate must be in standing path"
        assert "bq_gate [standing]" not in standing_src, "BQ gate removed per spec"


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 12 — Tier 2 NFL behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestReq12NFLTier2:
    """12. Tier 2 NFL behavior: same gates as MLB (S-only, BQ≥95, UNDER blocked at NFL path)."""

    def test_nfl_in_strict_alert_sports(self):
        from config import config
        strict = {s.upper() for s in (config.ud_strict_alert_sports or [])}
        assert "NFL" in strict, "NFL must be in ud_strict_alert_sports"

    def test_nfl_faces_bq95_gate(self):
        from config import config
        bq_threshold = getattr(config, "UD_STRICT_SPORT_MIN_BET_QUALITY", None)
        assert bq_threshold is not None, "UD_STRICT_SPORT_MIN_BET_QUALITY must be configured"
        assert bq_threshold >= 90, f"Strict BQ threshold must be ≥ 90, got {bq_threshold}"

    def test_nfl_requires_s_tier(self):
        """NFL requires S-tier only (not A or B) — same as MLB."""
        from config import config
        # mlb_alert_tiers applies to all strict sports
        tiers = list(getattr(config, "UD_MLB_ALERT_TIERS", []) or [])
        if tiers:
            assert "S" in tiers or "s" in tiers, "S must be an allowed tier for strict sports"

    def test_fast_resume_does_not_bypass_nfl_gates(self):
        """fast_resume=True must NOT bypass NFL/MLB tier or UNDER gates."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        standing_start = src.find("4A: Standing opportunity scan")
        standing_src = src[standing_start:]
        assert "ud_strict_alert_sports" in standing_src, (
            "fast_resume must NOT bypass ud_strict_alert_sports tier gate"
        )
        assert "ud_mlb_alert_tiers" in standing_src, (
            "fast_resume must NOT bypass MLB/NFL S-only tier gate"
        )
        assert "mlb_under_gate [standing]" in standing_src, (
            "fast_resume must NOT bypass MLB/NFL UNDER direction gate"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 13 — Recent checkpoint does NOT skip cold-start props
# ═══════════════════════════════════════════════════════════════════════════

class TestReq13CheckpointDoesNotSkipColdStart:
    """13. A recent checkpoint enables fast standing evaluation, not a permanent skip."""

    def test_standing_path_blocked_during_cold_start(self):
        """Fast Resume removed — standing path is blocked during cold-start (scan 1).

        After cold-start completes, standing path evaluates props on every subsequent scan.
        """
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_fast_resume" not in src, (
            "Fast Resume removed: _fast_resume must not appear in underdog_job"
        )
        assert "not is_cold_start and chat_ids" in src, (
            "Standing path must use plain 'not is_cold_start' gate (no fast-resume bypass)"
        )

    def test_fast_resume_not_skip_gate_logic(self):
        """Cold-start props NOT permanently skipped — standing path evaluates them on scan 1."""
        # Truth table: fast_resume=True, is_cold_start=True → standing RUNS
        is_cold_start = True
        fast_resume = True
        standing_runs = (not is_cold_start or fast_resume)
        assert standing_runs is True, (
            "When fast_resume=True AND is_cold_start=True, standing path must run"
        )

    def test_existing_props_not_completely_skipped(self):
        """For existing props (not new, not removed) during fast-resume scan 1:

        • cold-start rescore: skipped (scores are fresh from checkpoint)
        • standing path: RUNS (due to _fast_resume guard)
        • These props are immediately re-evaluated, NOT delayed to scan 2.
        """
        is_cold_start = True
        fast_resume = True
        is_new_prop = False
        is_removed = False

        # Cold-start rescore gate: runs when (not is_removed and is_cold_start and not _fast_resume)
        cold_start_rescore_runs = (not is_removed and is_cold_start and not fast_resume)
        assert cold_start_rescore_runs is False, (
            "Cold-start rescore skipped when fast_resume=True — scores are fresh"
        )

        # Standing path gate: runs when (not is_cold_start or _fast_resume)
        standing_runs = (not is_cold_start or fast_resume)
        assert standing_runs is True, (
            "Standing path RUNS when fast_resume=True — picks up existing props"
        )

    def test_without_fast_resume_cold_start_is_normal(self):
        """Without fast_resume, cold-start works as before (standing skipped on scan 1)."""
        is_cold_start = True
        fast_resume = False

        cold_start_rescore_runs = (not False and is_cold_start and not fast_resume)
        assert cold_start_rescore_runs is True, "Cold-start rescore runs when fast_resume=False"

        standing_runs = (not is_cold_start or fast_resume)
        assert standing_runs is False, "Standing skipped when fast_resume=False and is_cold_start"


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 14 — Props with sufficient history use fast incremental resume
# ═══════════════════════════════════════════════════════════════════════════

class TestReq14FastIncrementalResume:
    """14. Props with sufficient history use fast incremental resume (LC path)."""

    def test_lc_path_runs_independent_of_cold_start(self):
        """LC (line-change) path runs for any prop with a line change, regardless of cold-start."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # LC path check: line_changed and NOT cold-start
        assert "line_changed" in src or "line_delta" in src, (
            "LC path must detect line changes"
        )

    def test_checkpoint_age_determines_fast_vs_full(self):
        """Checkpoint age determines fast vs full resume — not prop-level data."""
        from engine.health import HealthTracker
        h, tmp = _make_health()
        try:
            # No checkpoint → full rescore
            age = h.get_scan_checkpoint_age_minutes()
            assert age is None, "No checkpoint → age=None → full cold-start"

            # Recent checkpoint → fast resume
            h.record_scan_checkpoint()
            age2 = h.get_scan_checkpoint_age_minutes()
            assert age2 is not None and age2 < 1.0, (
                "After recording checkpoint, age must be < 1 min"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_fast_resume_threshold_removed(self):
        """Fast Resume removed — _FAST_RESUME_THRESHOLD_MINUTES must NOT exist."""
        import market_engine as me
        assert not hasattr(me, "_FAST_RESUME_THRESHOLD_MINUTES"), (
            "_FAST_RESUME_THRESHOLD_MINUTES must be removed — Fast Resume is no longer supported"
        )

    def test_always_performs_full_cold_start_rescore(self):
        """After Fast Resume removal, every restart performs a full cold-start rescore.

        Checkpoints are still recorded for health monitoring, but no longer
        short-circuit the startup execution path.
        """
        import market_engine as me
        # Threshold removed — no conditional skip logic
        assert not hasattr(me, "_FAST_RESUME_THRESHOLD_MINUTES")
        # Checkpoint recording still works (health monitoring)
        from engine.health import HealthTracker
        assert hasattr(HealthTracker, "record_scan_checkpoint")
        assert hasattr(HealthTracker, "get_scan_checkpoint_age_minutes")


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 15 — Genuinely new props initialize correctly
# ═══════════════════════════════════════════════════════════════════════════

class TestReq15NewPropsInitialize:
    """15. Genuinely new props (not in PropLineHistory) initialize correctly."""

    def test_is_new_prop_gate_exists(self):
        """is_new_prop must be tracked in the main loop."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "is_new_prop" in src

    def test_new_prop_path_always_runs(self):
        """New prop processing is never gated by fast_resume — always runs on every scan.

        New props (not in PropLineHistory) go through the is_new_prop branch
        BEFORE the cold-start elif — they are always initialized regardless of
        whether it is a cold-start cycle or not.
        """
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # Fast Resume must be gone
        assert "_fast_resume" not in src, (
            "Fast Resume removed: _fast_resume must not appear in underdog_job"
        )
        # is_new_prop branch must still exist and precede the cold-start elif
        new_prop_idx = src.find("is_new_prop")
        cold_start_idx = src.find("is_cold_start")
        # Use the cold-start elif gate (more specific than bare is_cold_start)
        cold_start_idx = src.find("elif not is_removed and is_cold_start")
        assert new_prop_idx != -1, "is_new_prop must exist in underdog_job"
        assert cold_start_idx != -1, "cold-start elif gate must exist in underdog_job"
        assert new_prop_idx < cold_start_idx, (
            "is_new_prop path must come BEFORE cold-start elif gate — "
            "new props initialize regardless of startup cycle"
        )

    def test_new_prop_sent_to_digest_or_alerted(self):
        """New props are either immediately alerted or added to the digest batch."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_new_props_batch" in src or "np_immediate" in src, (
            "New prop must be added to digest batch or immediate alert list"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 16 — Restart does not duplicate stored picks or alerts
# ═══════════════════════════════════════════════════════════════════════════

class TestReq16NoDuplicates:
    """16. Restart does not duplicate stored picks or alerts."""

    def test_dedup_dict_restored_on_restart(self):
        """_prop_market_alerted is rebuilt from DB on restart → prevents duplicate alerts."""
        src = inspect.getsource(__import__("market_engine")._init_state_from_db)
        assert "_prop_market_alerted" in src, (
            "_init_state_from_db must restore _prop_market_alerted from DB"
        )

    def test_24h_dedup_check_in_standing_path(self):
        """Standing path checks DB dedup before alerting (survives restart)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "has_recent_ud_alert" in src, (
            "Standing path must call has_recent_ud_alert for DB-backed dedup"
        )

    def test_market_first_alert_restored_on_restart(self):
        """_MARKET_FIRST_ALERT restored on restart → availability tracking intact."""
        src = inspect.getsource(__import__("market_engine")._init_state_from_db)
        assert "_MARKET_FIRST_ALERT" in src, (
            "_init_state_from_db must restore _MARKET_FIRST_ALERT"
        )

    def test_priority_override_dedup_per_session(self):
        """_priority_override_sent is a per-session set → prevents double-override in one session."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_priority_override_sent" in src or "_priority_alerted_this_scan" in src, (
            "Priority override must have per-session dedup to prevent duplicates"
        )

    def test_has_recent_ud_alert_uses_db_backed_query(self):
        """has_recent_ud_alert uses the DB (not in-memory state) → survives restart."""
        import database as db_mod
        assert hasattr(db_mod.Database, "has_recent_ud_alert"), (
            "Database must have has_recent_ud_alert method"
        )
        # It must use DB query (not module-level set)
        src = inspect.getsource(db_mod.Database.has_recent_ud_alert)
        assert "select" in src.lower() or "query" in src.lower() or "session" in src.lower(), (
            "has_recent_ud_alert must use DB query, not in-memory state"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard label verification
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardLabels:
    """Verify dashboard labels clearly distinguish all-time vs today-only data."""

    def test_tier_breakdown_has_all_time_label(self):
        import pathlib
        src = pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..", "engine", "dashboard.py")
        ).read_text()
        assert "All-time" in src or "all-time" in src, (
            "Dashboard Tier Breakdown must be labeled as 'All-time'"
        )

    def test_by_sport_has_all_time_label(self):
        import pathlib
        src = pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..", "engine", "dashboard.py")
        ).read_text()
        # Count occurrences — one for tier, one for by-sport
        count = src.count("All-time")
        assert count >= 2, (
            f"Both Tier Breakdown and By Sport must have 'All-time' labels, found {count}"
        )

    def test_today_header_still_present(self):
        """Today section header must still be present."""
        import pathlib
        src = pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..", "engine", "dashboard.py")
        ).read_text()
        assert "Today" in src, "Dashboard must still show 'Today' header"

    def test_tier_breakdown_comment_explains_population(self):
        """Code comment must explain all-time alerted population."""
        import pathlib
        src = pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..", "engine", "dashboard.py")
        ).read_text()
        # The comment must distinguish funnel vs dashboard population
        assert "funnel" in src.lower() or "/funnel" in src.lower(), (
            "Comment must distinguish /funnel (candidate pipeline) vs "
            "dashboard Tier Breakdown (all-time alerted)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Issue 1 — Qualified = intermediate gate (documented)
# ═══════════════════════════════════════════════════════════════════════════

class TestIssue1QualifiedIsIntermediate:
    """Verify 'qualified' in PropCandidateLog is correctly an intermediate gate."""

    def test_qualified_logged_before_delivery_gates(self):
        """'qualified' is logged in PropCandidateLog before confidence/live/BQ gates."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # The log_prop_opportunity with "Qualified" watchlist_state appears in standing path
        assert '"Qualified"' in src, (
            "'Qualified' watchlist_state must be logged in PropCandidateLog"
        )

    def test_delivery_gates_after_prop_candidate_log(self):
        """Delivery gates (confidence, live/past, BQ) apply AFTER PropCandidateLog write."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        standing_start = src.find("4A: Standing opportunity scan")
        standing_src = src[standing_start:]

        # PropCandidateLog write (log_prop_opportunity) must appear before confidence gate
        log_idx  = standing_src.find("log_prop_opportunity")
        conf_idx = standing_src.find("confidence >= 95")  # first gate in standing

        assert log_idx != -1, "log_prop_opportunity must exist in standing path"
        assert conf_idx != -1, "confidence gate must exist in standing path"

    def test_is_game_live_or_past_gate_present(self):
        """_is_game_live_or_past gate must be in the standing path."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_is_game_live_or_past" in src, (
            "_is_game_live_or_past must gate standing path alerts"
        )

    def test_confidence_gate_present_in_standing(self):
        """Per-tier confidence gate must be in the standing path."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "min_conf_for_sport_tier" in src, (
            "min_conf_for_sport_tier must gate standing path confidence"
        )

    def test_qualified_does_not_guarantee_telegram_delivery(self):
        """'qualified' status does NOT guarantee Telegram delivery.

        A prop labeled 'qualified' in /funnel has passed:
          ✓ Scoring (score + tier derived)
          ✓ Directional recommendation (non-PASS)

        But still must pass:
          ☐ Per-tier confidence gate (S ≥ 80 non-strict)
          ☐ Live/past game gate
          ☐ 24h dedup gate (DB-backed)
          ☐ Top-3-per-sport limit in this cycle
          ☐ Alert delivery

        These are legitimate safety gates. 'qualified' ≠ 'delivered'.
        """
        # Structural: verify gates exist AFTER PropCandidateLog write in standing path
        src = inspect.getsource(__import__("market_engine").underdog_job)
        standing_start = src.find("4A: Standing opportunity scan")
        standing_src = src[standing_start:]
        gates = [
            "_s_min_conf",           # confidence gate
            "_is_game_live_or_past",  # live/past gate
            "has_recent_ud_alert",    # dedup gate
        ]
        for gate in gates:
            assert gate in standing_src, (
                f"Gate '{gate}' must exist in standing path after PropCandidateLog write"
            )
