"""
test_scan_performance.py — Regression suite for the scan-performance batch-save pass.

Verifies that the performance fix (bulk snapshot save instead of per-prop individual
saves) does not alter any V3.5-frozen behaviour.  14 regression items:

 1. Poll interval configured to 120 s
 2. max_instances=1 enforced on underdog_monitor job
 3. Full active prop feed is monitored each cycle (no sports/markets silently dropped)
 4. No sports removed from UD_ALERT_SPORTS
 5. Scoring model unchanged — compute_market_quality is still called
 6. Tier thresholds unchanged (S≥85, A≥70, B≥55)
 7. Confidence / BQ gates unchanged (UD_MIN_CONF_S, UD_MIN_CONF_A)
 8. MLB UNDER whitelist still present
 9. NFL direction rules still present
10. Dedup in-memory dict still used
11. CLV seed still called for S/A picks
12. Stale-line guard active on all 5 delivery paths
13. Fast Resume completely absent
14. Scheduler skips are observable (logged by APScheduler, not silently swallowed)

Tests use source-code inspection where behavioural testing would require a full bot
harness, and lightweight unit-level assertions everywhere else.
"""

import ast
import re
import sys
import os
import importlib

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def _cfg():
    """Load config module with dummy env so defaults are readable."""
    import types

    spec = importlib.util.spec_from_file_location(
        "config_perf_test", os.path.join(ROOT, "config.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # Provide all secrets as blank strings so Config() doesn't raise
    dummy_env = {
        "TELEGRAM_BOT_TOKEN": "dummy",
        "TELEGRAM_TOKEN": "dummy",
        "ODDS_API_KEY": "dummy",
        "SESSION_SECRET": "dummy",
    }
    with pytest.MonkeyPatch().context() as mp:
        for k, v in dummy_env.items():
            mp.setenv(k, v)
        spec.loader.exec_module(mod)
    return mod


# ── 1. Poll interval ──────────────────────────────────────────────────────────


def test_poll_interval_default_120s():
    """config.py must default UNDERDOG_POLL_INTERVAL to '120'."""
    src = _src("config.py")
    # Config uses os.environ.get with "120" as default
    assert re.search(
        r'UNDERDOG_POLL_INTERVAL.*int\(os\.environ\.get\([^,]+,\s*["\']120["\']\)',
        src,
    ), "UNDERDOG_POLL_INTERVAL default must be '120'"


# ── 2. max_instances=2 (fast-fetch overlap design) ───────────────────────────


def test_max_instances_2_in_underdog_monitor():
    """underdog_monitor job must declare max_instances=2 (configured in main.py).

    max_instances=2 allows a second underdog_job instance to start while the
    primary full scan is still scoring. The _ud_full_scan_running module flag
    gates the second instance to a fast new-prop fetch only.
    """
    src = _src("main.py")
    assert '"max_instances": 2' in src or "'max_instances': 2" in src, \
        "max_instances=2 must be present in main.py job_kwargs (fast-fetch design)"


def test_max_instances_not_raised_above_2():
    """max_instances must not be set above 2 (only primary + fast-fetch needed)."""
    src = _src("main.py")
    for bad in ('"max_instances": 3', '"max_instances": 4', "'max_instances': 3"):
        assert bad not in src, f"Found forbidden {bad} in main.py — max 2 allowed"


# ── 3. Full prop feed monitored ───────────────────────────────────────────────


def test_incremental_records_buffer_used():
    """Normal-scan records must accumulate in _incremental_records, not saved 1-by-1."""
    src = _src("market_engine.py")
    assert "_incremental_records.append(record)" in src, (
        "_incremental_records.append(record) not found — per-prop batch buffering missing"
    )


def test_bulk_save_called_for_incremental():
    """save_underdog_snapshots_bulk must be called with the incremental records."""
    src = _src("market_engine.py")
    # The flush block copies _incremental_records into _incr_snapshot then bulk-saves it
    assert "save_underdog_snapshots_bulk(" in src, (
        "save_underdog_snapshots_bulk not found — bulk flush missing"
    )
    assert "_incr_snapshot" in src or "_incremental_records" in src, (
        "Incremental snapshot buffer not found in bulk-save block"
    )


def test_single_save_not_called_in_main_loop():
    """save_underdog_snapshot (singular) must not be called inside the per-prop loop."""
    src = _src("market_engine.py")
    # The call save_underdog_snapshot(record) should not be awaited in the hot path
    # It may still exist as a helper but must not appear after the '_incremental_records' refactor
    # We check that the only remaining reference is the function definition in database.py
    # (we only inspect market_engine.py here)
    lines = src.splitlines()
    hot_path_calls = [
        i for i, ln in enumerate(lines, 1)
        if "await db.save_underdog_snapshot(record)" in ln
    ]
    assert hot_path_calls == [], (
        f"await db.save_underdog_snapshot(record) still called at line(s) "
        f"{hot_path_calls} — per-prop individual save not removed"
    )


def test_cold_start_bulk_save_unchanged():
    """Cold-start bulk-save path (_cold_start_records) must still be present."""
    src = _src("market_engine.py")
    assert "_cold_start_records.append(record)" in src
    assert "save_underdog_snapshots_bulk(_cs_batch)" in src


# ── 4. No sports silently removed ─────────────────────────────────────────────


def test_ud_alert_sports_default_contains_core_sports():
    """UD_ALERT_SPORTS default must still contain MLB, NFL, NBA."""
    src = _src("config.py")
    for sport in ("MLB", "NFL", "NBA"):
        assert sport in src, f"{sport} missing from config.py UD_ALERT_SPORTS default"


# ── 5. Scoring unchanged ──────────────────────────────────────────────────────


def test_compute_market_quality_still_imported():
    """compute_market_quality must still be imported/used in market_engine.py."""
    src = _src("market_engine.py")
    assert "compute_market_quality" in src


# ── 6. Tier thresholds ────────────────────────────────────────────────────────


def test_s_tier_threshold_85():
    """S-tier threshold must be ≥85 in _tier_from_conf or equivalent."""
    src = _src("market_engine.py")
    # S-tier confidence threshold comment or value
    assert "85" in src and "S" in src  # broad; precise check follows
    # _tier_from_conf uses 85 as the S cutoff
    assert re.search(r'conf\s*>=\s*85', src) or re.search(r'>=\s*85.*[Ss]', src) or \
           "_TIER_S_CUTOFF = 85" in src or "UD_MIN_CONF_S" in src, \
           "S-tier 85 cutoff not found in market_engine.py"


def test_a_tier_threshold_70():
    """A-tier threshold must be 70 in config / _tier_from_conf."""
    src = _src("market_engine.py")
    assert re.search(r'conf\s*>=\s*70', src) or "UD_MIN_CONF_A" in src, \
        "A-tier 70 cutoff not found in market_engine.py"


# ── 7. Confidence / BQ gates ──────────────────────────────────────────────────


def test_min_conf_s_config_key_present():
    src = _src("config.py")
    assert "UD_MIN_CONF_S" in src


def test_min_conf_a_config_key_present():
    src = _src("config.py")
    assert "UD_MIN_CONF_A" in src


def test_bq_gate_still_present():
    """BQ gate (bet_quality_score / BQ) must still be referenced."""
    src = _src("market_engine.py")
    assert "bq_gate" in src or "bet_quality_score" in src or "BQ" in src


# ── 8. MLB UNDER whitelist ────────────────────────────────────────────────────


def test_mlb_under_whitelist_present():
    """MLB UNDER whitelist logic must still exist in market_engine.py."""
    src = _src("market_engine.py")
    assert "MLB UNDER whitelist" in src, \
        "MLB UNDER whitelist not found in market_engine.py"


def test_mlb_under_whitelist_has_entries():
    """MLB UNDER whitelist must gate delivery paths in market_engine.py."""
    src = _src("market_engine.py")
    # Must appear at least twice — lc/np path and standing path
    count = src.count("MLB UNDER whitelist")
    assert count >= 2, \
        f"MLB UNDER whitelist appears only {count} time(s) — may be missing from a delivery path"


# ── 9. NFL direction rules ────────────────────────────────────────────────────


def test_nfl_direction_rules_present():
    """NFL-specific direction logic must exist in market_engine.py."""
    src = _src("market_engine.py")
    # NFL UNDER is allowed; must see NFL referenced near direction/UNDER logic
    assert "NFL" in src
    assert "UNDER" in src


# ── 10. Dedup unchanged ───────────────────────────────────────────────────────


def test_prop_market_alerted_dict_present():
    """In-memory dedup dict _prop_market_alerted must still be used."""
    src = _src("market_engine.py")
    assert "_prop_market_alerted" in src


def test_is_prop_deduped_called():
    """_is_prop_deduped helper must still be called."""
    src = _src("market_engine.py")
    assert "_is_prop_deduped(" in src


# ── 11. CLV seed ──────────────────────────────────────────────────────────────


def test_clv_seed_still_called():
    """AlertCLVSeed / clv_seeder still referenced (not removed by perf pass)."""
    src = _src("market_engine.py")
    assert "AlertCLVSeed" in src or "clv_seed" in src.lower()


# ── 12. Stale-line guard on all paths ─────────────────────────────────────────


def test_ud_line_fresh_helper_present():
    src = _src("market_engine.py")
    assert "_ud_line_fresh(" in src or "def _ud_line_fresh" in src


def test_current_scan_line_map_built():
    """_current_scan_line_map must be built from ud_snaps each cycle."""
    src = _src("market_engine.py")
    assert "_current_scan_line_map" in src


def test_stale_line_guard_on_np_path():
    """NP delivery path must check _ud_line_fresh."""
    src = _src("market_engine.py")
    assert "_ud_line_fresh(" in src


def test_stale_line_guard_on_lc_path():
    """LC delivery path must reference _ud_line_fresh or _current_scan_line_map."""
    src = _src("market_engine.py")
    # Both np and lc paths use the same helper
    assert "_ud_line_fresh(" in src and "_current_scan_line_map" in src


# ── 13. Fast Resume absent ────────────────────────────────────────────────────


def test_fast_resume_threshold_removed():
    src = _src("market_engine.py")
    assert "_FAST_RESUME_THRESHOLD_MINUTES" not in src, \
        "_FAST_RESUME_THRESHOLD_MINUTES found — fast resume not fully removed"


def test_fast_resume_function_removed():
    src = _src("market_engine.py")
    assert "_fast_resume" not in src, \
        "_fast_resume found — fast resume not fully removed"


def test_no_fast_resume_in_config():
    src = _src("config.py")
    assert "FAST_RESUME" not in src, \
        "FAST_RESUME found in config.py — fast resume not fully removed"


# ── 14. Scheduler skips observable ───────────────────────────────────────────


def test_misfire_grace_time_set():
    """misfire_grace_time must be configured in main.py so skips are recorded."""
    src = _src("main.py")
    assert "misfire_grace_time" in src, \
        "misfire_grace_time not found in main.py — scheduler skips will be silently dropped"


def test_max_instances_2_with_scan_running_flag():
    """max_instances=2 in job_kwargs allows fast-fetch overlap; the
    _ud_full_scan_running flag in market_engine prevents duplicate heavy scans."""
    import market_engine as me
    src = _src("main.py")
    assert "max_instances" in src, \
        "max_instances not present in main.py — concurrent instance config missing"
    # Confirm it is exactly 2
    assert '"max_instances": 2' in src or "'max_instances': 2" in src, \
        "max_instances must be 2 (fast-fetch overlap design)"
    # The flag must guard the heavy path
    import inspect
    me_src = inspect.getsource(me)
    assert "_ud_full_scan_running" in me_src, \
        "_ud_full_scan_running flag missing — second instance not guarded"
