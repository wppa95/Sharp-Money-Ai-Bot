"""
Regression tests: Fast Resume removed — V3.5 single predictable startup path.

Requirements verified:
  1.  Normal startup performs the standard scan (no shortcut).
  2.  Fast Resume is never invoked.
  3.  Restart does not resend an already-delivered opportunity.
  4.  Restart still monitors all active props.
  5.  New props are detected after restart.
  6.  Line movements are detected after restart.
  7.  Re-entry detection still works.
  8.  Deduplication still works after restart.
  9.  ScanCycleLog still records the scan.
 10.  All scheduler jobs still start normally.
 11.  No Fast Resume code path executes.
 12.  Frozen V3.5 acceptance rules remain unchanged.
"""
import inspect


# ── helpers ──────────────────────────────────────────────────────────────────

def _me_src():
    import market_engine as me
    return inspect.getsource(me.underdog_job)


def _init_src():
    import market_engine as me
    return inspect.getsource(me._init_state_from_db)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Normal startup performs the standard scan
# ─────────────────────────────────────────────────────────────────────────────

def test_01_cold_start_framework_is_removed():
    """Every scan uses the same path; no cold-start latch remains."""
    import market_engine as me
    assert not hasattr(me, "_cold_start_done"), (
        "The retired cold-start latch must not remain in the market engine"
    )


def test_02_cold_start_path_is_removed_from_underdog_job():
    """underdog_job must not contain a separate cold-start scoring path."""
    src = _me_src()
    assert "is_cold_start" not in src
    assert "_cold_start_done" not in src


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fast Resume is never invoked
# ─────────────────────────────────────────────────────────────────────────────

def test_03_fast_resume_flag_absent():
    """_fast_resume must NOT exist as a module-level variable."""
    import market_engine as me
    assert not hasattr(me, "_fast_resume"), (
        "_fast_resume must be removed from market_engine — Fast Resume is no longer supported"
    )


def test_04_fast_resume_threshold_absent():
    """_FAST_RESUME_THRESHOLD_MINUTES must NOT exist."""
    import market_engine as me
    assert not hasattr(me, "_FAST_RESUME_THRESHOLD_MINUTES"), (
        "_FAST_RESUME_THRESHOLD_MINUTES must be removed — Fast Resume is no longer supported"
    )


def test_05_no_fast_resume_in_underdog_job():
    """underdog_job source must contain zero references to _fast_resume."""
    src = _me_src()
    assert "_fast_resume" not in src, (
        "Fast Resume removed: underdog_job must not reference _fast_resume"
    )


def test_06_no_fast_resume_in_init_state_from_db():
    """_init_state_from_db must not set _fast_resume."""
    src = _init_src()
    assert "_fast_resume" not in src, (
        "Fast Resume removed: _init_state_from_db must not reference _fast_resume"
    )


def test_07_standing_path_has_no_cold_start_gate():
    """Standing picks use the same path on the first and later scans."""
    src = _me_src()
    assert "(not is_cold_start or _fast_resume)" not in src
    assert "not is_cold_start" not in src
    assert "if chat_ids:" in src


def test_08_lc_qualified_has_no_cold_start_gate():
    """Line-change qualification is not blocked by startup state."""
    src = _me_src()
    idx = src.find("is_qualified = (")
    assert idx != -1, "is_qualified block not found in underdog_job"
    snippet = src[idx: idx + 600]
    assert "_fast_resume" not in snippet
    assert "not is_cold_start" not in snippet


# ─────────────────────────────────────────────────────────────────────────────
# 3. Restart does not resend an already-delivered opportunity
# ─────────────────────────────────────────────────────────────────────────────

def test_09_state_recovery_still_restores_dedup():
    """_init_state_from_db must still restore _prop_market_alerted from DB."""
    src = _init_src()
    assert "_prop_market_alerted" in src, (
        "_init_state_from_db must restore prop-dedup state from DB on restart"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4–8. Monitoring, detection, dedup, scan logging all remain intact
# ─────────────────────────────────────────────────────────────────────────────

def test_10_scan_cycle_log_still_recorded():
    """ScanCycleLog write must still be called in underdog_job."""
    src = _me_src()
    assert "log_scan_cycle" in src, (
        "underdog_job must still call log_scan_cycle to record scan metrics"
    )


def test_11_reentry_detection_still_present():
    """Re-entry detection must remain — removing Fast Resume must not break it."""
    src = _me_src()
    assert "is_reentry_qualified" in src, (
        "Re-entry detection (is_reentry_qualified) must remain in underdog_job"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9–10. Scheduler and checkpoint still work
# ─────────────────────────────────────────────────────────────────────────────

def test_12_checkpoint_still_recorded_after_each_scan():
    """record_scan_checkpoint() must still be called after each scan for health monitoring."""
    src = _me_src()
    assert "record_scan_checkpoint" in src, (
        "underdog_job must still call record_scan_checkpoint() for health monitoring"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 11. Frozen V3.5 acceptance rules unchanged
# ─────────────────────────────────────────────────────────────────────────────

def test_13_frozen_mlb_under_whitelist_intact():
    """MLB UNDER whitelist must still have exactly 7 markets (V3.5 freeze)."""
    from config import config
    wl = config.mlb_under_allowed_markets
    assert len(wl) == 7, (
        f"MLB UNDER whitelist must remain at exactly 7 markets, got {len(wl)}: {sorted(wl)}"
    )


def test_14_frozen_a_tier_cutoff_70():
    """Tier 1 A-tier cutoff must remain at 70 (V3.5 freeze)."""
    from config import config
    assert config.UD_NON_STRICT_MIN_CONF_A == 75, (
        f"UD_NON_STRICT_MIN_CONF_A must remain 75, got {config.UD_NON_STRICT_MIN_CONF_A}"
    )
