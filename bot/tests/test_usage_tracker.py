"""
tests/test_usage_tracker.py — Unit tests for ApiUsageTracker.

Coverage:
  - CallPriority enum ordering
  - infer_call_priority rules
  - record_request: count increments, threshold detection
  - should_allow: priority × budget-pct blocking matrix
  - should_allow: active-sport filter
  - get_stats: UsageStats fields including budget_bar
  - JSON persistence: save → load round-trip
  - _prune: old entries removed
  - monthly warning flag reset
  - singleton: init_usage_tracker / get_usage_tracker
  - UsageStats properties (is_over_budget, remaining_estimate, budget_bar)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# Make sure the bot package root is on the path when running from project root
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.usage_tracker import (
    WARN_THRESHOLDS,
    ApiUsageTracker,
    CallPriority,
    UsageStats,
    get_usage_tracker,
    infer_call_priority,
    init_usage_tracker,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_tracker(
    budgets: dict[str, int] | None = None,
    tmp_dir: str | None = None,
) -> ApiUsageTracker:
    if budgets is None:
        budgets = {"OddsAPI": 100}
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    # Patch out health monitor so tests don't require it
    with patch("providers.usage_tracker.ApiUsageTracker._get_authoritative_pct") as _mock:
        # We override this in individual tests where needed
        pass
    return ApiUsageTracker(monthly_budgets=budgets, data_dir=tmp_dir)


def _tracker_with_own_count_pct(budgets=None, tmp_dir=None) -> ApiUsageTracker:
    """
    Return a tracker that uses its own month_count for pct (no health monitor).
    Achieved by patching the health monitor import inside _get_authoritative_pct
    to always fail so it falls through to the own-count branch.
    """
    if budgets is None:
        budgets = {"OddsAPI": 100}
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    t = ApiUsageTracker.__new__(ApiUsageTracker)
    # Bypass __init__ to avoid file I/O; set state manually
    t._budgets        = dict(budgets)
    t._data_file      = os.path.join(tmp_dir, "api_usage.json")
    t._daily          = {}
    t._last_request   = {}
    t._warned         = {}
    t._season_checker = None
    t._current_month  = date.today().strftime("%Y-%m")
    return t


# ── CallPriority ───────────────────────────────────────────────────────────────

class TestCallPriority:
    def test_ordering(self):
        """Lower value = higher priority."""
        assert CallPriority.CRITICAL < CallPriority.HIGH
        assert CallPriority.HIGH     < CallPriority.MEDIUM
        assert CallPriority.MEDIUM   < CallPriority.LOW

    def test_int_values(self):
        assert CallPriority.CRITICAL == 1
        assert CallPriority.HIGH     == 2
        assert CallPriority.MEDIUM   == 3
        assert CallPriority.LOW      == 4


# ── infer_call_priority ────────────────────────────────────────────────────────

class TestInferCallPriority:
    def test_player_props_high(self):
        assert infer_call_priority("basketball_nba", "player_props") == CallPriority.HIGH

    def test_player_props_overrides_sport(self):
        # Even for a low-priority sport, player_props → HIGH
        assert infer_call_priority("soccer_epl", "player_props,h2h") == CallPriority.HIGH

    def test_mlb_high(self):
        # MLB is in active_sports by default; priority is config-driven, not hardcoded.
        assert infer_call_priority("baseball_mlb", "h2h") == CallPriority.HIGH

    def test_nba_low_when_not_in_active_sports(self):
        # NBA is not in the default active_sports config → LOW (not MEDIUM).
        # Priority is sport-agnostic: NBA would be HIGH only if added to active_sports.
        assert infer_call_priority("basketball_nba", "h2h,spreads") == CallPriority.LOW

    def test_nfl_low(self):
        assert infer_call_priority("americanfootball_nfl", "h2h") == CallPriority.LOW

    def test_epl_low(self):
        assert infer_call_priority("soccer_epl", "h2h,totals") == CallPriority.LOW

    def test_nhl_low(self):
        assert infer_call_priority("icehockey_nhl", "h2h,spreads") == CallPriority.LOW


# ── record_request ─────────────────────────────────────────────────────────────

class TestRecordRequest:
    def test_increments_today_count(self):
        t = _tracker_with_own_count_pct()
        for _ in range(3):
            t.record_request("OddsAPI", CallPriority.MEDIUM)
        today_str = date.today().isoformat()
        assert t._daily["OddsAPI"][today_str] == 3

    def test_increments_month_count(self):
        t = _tracker_with_own_count_pct()
        t.record_request("OddsAPI", CallPriority.MEDIUM)
        t.record_request("OddsAPI", CallPriority.LOW)
        assert t._get_month_count("OddsAPI") == 2

    def test_updates_last_request(self):
        t = _tracker_with_own_count_pct()
        assert t._last_request.get("OddsAPI") is None
        t.record_request("OddsAPI", CallPriority.LOW)
        assert t._last_request["OddsAPI"] is not None

    def test_tracks_multiple_providers(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 100, "PP": 0})
        t.record_request("OddsAPI", CallPriority.MEDIUM)
        t.record_request("PP", CallPriority.CRITICAL)
        assert t._get_today_count("OddsAPI") == 1
        assert t._get_today_count("PP") == 1

    def test_returns_first_crossed_threshold_75(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 100})
        # Push to 74 requests (74%) — no threshold
        for _ in range(74):
            result = t.record_request("OddsAPI", CallPriority.LOW)
        assert result is None
        # 75th request crosses 75%
        result = t.record_request("OddsAPI", CallPriority.LOW)
        assert result == 75

    def test_returns_none_after_threshold_already_warned(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 100})
        for _ in range(75):
            t.record_request("OddsAPI", CallPriority.LOW)
        # 75 already returned; next call should not re-fire 75
        result = t.record_request("OddsAPI", CallPriority.LOW)
        assert result is None  # 76% — threshold 75 already warned, 90 not yet

    def test_returns_90_threshold(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 100})
        for _ in range(89):
            t.record_request("OddsAPI", CallPriority.LOW)
        result = t.record_request("OddsAPI", CallPriority.LOW)
        assert result == 90

    def test_returns_100_threshold(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 100})
        for _ in range(99):
            t.record_request("OddsAPI", CallPriority.LOW)
        result = t.record_request("OddsAPI", CallPriority.LOW)
        assert result == 100

    def test_persists_to_disk(self):
        tmp = tempfile.mkdtemp()
        t = _tracker_with_own_count_pct(tmp_dir=tmp)
        t._data_file = os.path.join(tmp, "api_usage.json")
        t.record_request("OddsAPI", CallPriority.MEDIUM)
        t._save()
        with open(t._data_file) as fh:
            data = json.load(fh)
        today_str = date.today().isoformat()
        assert data["daily"]["OddsAPI"][today_str] == 1


# ── should_allow ───────────────────────────────────────────────────────────────

class TestShouldAllow:
    """Budget enforcement + sport filter."""

    def _at_pct(self, pct: float, budget: int = 100):
        """Tracker where own-count gives exactly *pct*% budget used."""
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": budget})
        count = int(pct * budget / 100)
        today = date.today().isoformat()
        t._daily.setdefault("OddsAPI", {})[today] = count
        return t

    # ── unlimited budget ──────────────────────────────────────────────────────

    def test_unlimited_budget_always_allowed(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 0})
        allowed, reason = t.should_allow("OddsAPI", CallPriority.LOW)
        assert allowed

    def test_unlisted_provider_always_allowed(self):
        t = _tracker_with_own_count_pct(budgets={})
        allowed, reason = t.should_allow("OtherAPI", CallPriority.LOW)
        assert allowed

    # ── under 90 % — all priorities pass ─────────────────────────────────────

    def test_below_75_all_allowed(self):
        t = self._at_pct(50)
        for pri in (CallPriority.CRITICAL, CallPriority.HIGH,
                    CallPriority.MEDIUM, CallPriority.LOW):
            allowed, _ = t.should_allow("OddsAPI", pri)
            assert allowed, f"{pri.name} should be allowed at 50%"

    def test_at_75_all_allowed(self):
        t = self._at_pct(75)
        for pri in (CallPriority.CRITICAL, CallPriority.HIGH,
                    CallPriority.MEDIUM, CallPriority.LOW):
            allowed, _ = t.should_allow("OddsAPI", pri)
            assert allowed, f"{pri.name} should be allowed at 75%"

    def test_at_89_all_allowed(self):
        t = self._at_pct(89)
        for pri in (CallPriority.CRITICAL, CallPriority.HIGH,
                    CallPriority.MEDIUM, CallPriority.LOW):
            allowed, _ = t.should_allow("OddsAPI", pri)
            assert allowed

    # ── 90–99 % — LOW blocked ─────────────────────────────────────────────────

    def test_at_90_low_blocked(self):
        t = self._at_pct(90)
        allowed, reason = t.should_allow("OddsAPI", CallPriority.LOW)
        assert not allowed
        assert "LOW" in reason

    def test_at_90_medium_allowed(self):
        t = self._at_pct(90)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.MEDIUM)
        assert allowed

    def test_at_90_high_allowed(self):
        t = self._at_pct(90)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.HIGH)
        assert allowed

    def test_at_90_critical_allowed(self):
        t = self._at_pct(90)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.CRITICAL)
        assert allowed

    # ── ≥ 100 % — LOW + MEDIUM blocked; HIGH + CRITICAL pass ────────────────

    def test_at_100_low_blocked(self):
        t = self._at_pct(100)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.LOW)
        assert not allowed

    def test_at_100_medium_blocked(self):
        t = self._at_pct(100)
        allowed, reason = t.should_allow("OddsAPI", CallPriority.MEDIUM)
        assert not allowed
        assert "MEDIUM" in reason or "exhausted" in reason.lower()

    def test_at_100_high_still_allowed(self):
        t = self._at_pct(100)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.HIGH)
        assert allowed

    def test_at_100_critical_still_allowed(self):
        t = self._at_pct(100)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.CRITICAL)
        assert allowed

    # ── Active-sport filter ───────────────────────────────────────────────────

    def test_sport_filter_blocks_out_of_season(self):
        t = _tracker_with_own_count_pct()
        checker = MagicMock()
        checker.get_active_sport_keys.return_value = frozenset({"baseball_mlb"})
        t.set_season_checker(checker)
        allowed, reason = t.should_allow(
            "OddsAPI", CallPriority.MEDIUM, sport_key="basketball_nba",
        )
        assert not allowed
        assert "basketball_nba" in reason

    def test_sport_filter_allows_in_season(self):
        t = _tracker_with_own_count_pct()
        checker = MagicMock()
        checker.get_active_sport_keys.return_value = frozenset({"baseball_mlb", "basketball_nba"})
        t.set_season_checker(checker)
        allowed, _ = t.should_allow(
            "OddsAPI", CallPriority.MEDIUM, sport_key="basketball_nba",
        )
        assert allowed

    def test_sport_filter_fail_open_when_cache_empty(self):
        """Empty frozenset from checker → fail-open (allow)."""
        t = _tracker_with_own_count_pct()
        checker = MagicMock()
        checker.get_active_sport_keys.return_value = frozenset()
        t.set_season_checker(checker)
        allowed, _ = t.should_allow(
            "OddsAPI", CallPriority.MEDIUM, sport_key="basketball_nba",
        )
        assert allowed

    def test_sport_filter_skipped_for_high_priority(self):
        """HIGH priority calls bypass the sport filter."""
        t = _tracker_with_own_count_pct()
        checker = MagicMock()
        checker.get_active_sport_keys.return_value = frozenset({"baseball_mlb"})
        t.set_season_checker(checker)
        # basketball_nba NOT in active keys, but priority is HIGH → should pass
        allowed, _ = t.should_allow(
            "OddsAPI", CallPriority.HIGH, sport_key="basketball_nba",
        )
        assert allowed


# ── get_stats / UsageStats ────────────────────────────────────────────────────

class TestGetStats:
    def _simple_tracker(self, count: int = 0, budget: int = 200) -> ApiUsageTracker:
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": budget})
        if count:
            today = date.today().isoformat()
            t._daily["OddsAPI"] = {today: count}
        return t

    def test_today_count(self):
        t = self._simple_tracker(count=7)
        assert t.get_stats("OddsAPI").today_count == 7

    def test_month_count(self):
        t = self._simple_tracker(count=42)
        assert t.get_stats("OddsAPI").month_count == 42

    def test_month_budget(self):
        t = self._simple_tracker(budget=500)
        assert t.get_stats("OddsAPI").month_budget == 500

    def test_budget_pct_calculation(self):
        t = self._simple_tracker(count=50, budget=200)
        stats = t.get_stats("OddsAPI")
        assert abs(stats.budget_pct - 25.0) < 0.01

    def test_warning_level_none_initially(self):
        t = self._simple_tracker()
        assert t.get_stats("OddsAPI").warning_level is None

    def test_warning_level_after_threshold_crossed(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 100})
        t._warned["OddsAPI"] = {75, 90}
        stats = t.get_stats("OddsAPI")
        assert stats.warning_level == 90

    def test_is_over_budget_false(self):
        t = self._simple_tracker(count=50, budget=200)
        assert not t.get_stats("OddsAPI").is_over_budget

    def test_is_over_budget_true(self):
        t = self._simple_tracker(count=200, budget=200)
        stats = t.get_stats("OddsAPI")
        assert stats.is_over_budget

    def test_budget_bar_empty(self):
        t = self._simple_tracker(count=0, budget=100)
        assert t.get_stats("OddsAPI").budget_bar == "░" * 10

    def test_budget_bar_full(self):
        t = self._simple_tracker(count=100, budget=100)
        assert t.get_stats("OddsAPI").budget_bar == "█" * 10

    def test_budget_bar_half(self):
        t = self._simple_tracker(count=50, budget=100)
        bar = t.get_stats("OddsAPI").budget_bar
        assert bar.count("█") == 5
        assert bar.count("░") == 5

    def test_remaining_estimate_with_budget(self):
        t = self._simple_tracker(count=30, budget=100)
        stats = t.get_stats("OddsAPI")
        # no health monitor → uses own count: 100 - 30 = 70
        assert stats.remaining_estimate == 70

    def test_remaining_estimate_unlimited(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 0})
        assert t.get_stats("OddsAPI").remaining_estimate is None

    def test_get_all_stats_includes_all_providers(self):
        t = _tracker_with_own_count_pct(budgets={"OddsAPI": 500, "PP": 0})
        stats = t.get_all_stats()
        assert "OddsAPI" in stats
        assert "PP" in stats


# ── JSON persistence ───────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load(self):
        tmp = tempfile.mkdtemp()
        t1 = _tracker_with_own_count_pct(tmp_dir=tmp)
        t1._data_file = os.path.join(tmp, "api_usage.json")
        today = date.today().isoformat()
        t1._daily["OddsAPI"] = {today: 42}
        t1._save()

        # New tracker loads from same file
        t2 = ApiUsageTracker(monthly_budgets={"OddsAPI": 100}, data_dir=tmp)
        assert t2._get_today_count("OddsAPI") == 42

    def test_load_missing_file_is_noop(self):
        tmp = tempfile.mkdtemp()
        # Should not raise; starts with empty counts
        t = ApiUsageTracker(monthly_budgets={"OddsAPI": 100}, data_dir=tmp)
        assert t._get_today_count("OddsAPI") == 0

    def test_record_request_triggers_save(self):
        tmp = tempfile.mkdtemp()
        t = _tracker_with_own_count_pct(tmp_dir=tmp)
        t._data_file = os.path.join(tmp, "api_usage.json")
        t.record_request("OddsAPI", CallPriority.LOW)
        assert os.path.exists(t._data_file)


# ── _prune ────────────────────────────────────────────────────────────────────

class TestPrune:
    def test_removes_old_entries(self):
        t = _tracker_with_own_count_pct()
        from datetime import timedelta
        old_date = (date.today() - timedelta(days=70)).isoformat()
        recent   = date.today().isoformat()
        t._daily["OddsAPI"] = {old_date: 10, recent: 5}
        t._prune(keep_days=65)
        assert old_date not in t._daily["OddsAPI"]
        assert recent in t._daily["OddsAPI"]

    def test_keeps_recent_entries(self):
        t = _tracker_with_own_count_pct()
        from datetime import timedelta
        d30 = (date.today() - timedelta(days=30)).isoformat()
        t._daily["OddsAPI"] = {d30: 99}
        t._prune(keep_days=65)
        assert d30 in t._daily["OddsAPI"]


# ── Monthly warning reset ─────────────────────────────────────────────────────

class TestMonthlyReset:
    def test_reset_monthly_warned_clears_flags(self):
        t = _tracker_with_own_count_pct()
        t._warned["OddsAPI"] = {75, 90}
        t.reset_monthly_warned("OddsAPI")
        assert "OddsAPI" not in t._warned

    def test_roll_month_clears_all_flags(self):
        t = _tracker_with_own_count_pct()
        t._warned["OddsAPI"] = {75}
        t._current_month = "2025-01"   # fake a previous month
        t._roll_month_if_needed()
        assert not t._warned.get("OddsAPI")


# ── Singleton ─────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_init_returns_tracker(self):
        tmp = tempfile.mkdtemp()
        with patch("providers.usage_tracker._DATA_DIR_DEFAULT", tmp):
            tracker = init_usage_tracker({"OddsAPI": 500}, data_dir=tmp)
        assert isinstance(tracker, ApiUsageTracker)

    def test_get_returns_same_instance(self):
        tmp = tempfile.mkdtemp()
        with patch("providers.usage_tracker._DATA_DIR_DEFAULT", tmp):
            t1 = init_usage_tracker({"OddsAPI": 500}, data_dir=tmp)
            t2 = get_usage_tracker()
        assert t1 is t2

    def test_get_before_init_returns_none_or_previous(self):
        # After any test runs init, get returns something.
        # Before any call it may be None — just confirm it doesn't raise.
        result = get_usage_tracker()
        assert result is None or isinstance(result, ApiUsageTracker)


# ── WARN_THRESHOLDS constant ──────────────────────────────────────────────────

def test_warn_thresholds_contains_expected():
    assert 75  in WARN_THRESHOLDS
    assert 90  in WARN_THRESHOLDS
    assert 100 in WARN_THRESHOLDS
