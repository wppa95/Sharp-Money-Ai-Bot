"""
Tests for providers/health_monitor.py — ProviderHealthMonitor.

Covers:
  - register() pre-creates an entry in DISABLED state
  - record_success() resets consecutive_failures and clears failure_type
  - record_failure() increments consecutive_failures and sets failure_type
  - Status derivation: OK → DEGRADED → DOWN thresholds
  - QUOTA and BLOCKED are sticky overrides regardless of failure count
  - is_quota_exhausted / is_blocked / is_healthy helpers
  - get_all_health() returns all registered providers
  - quota_remaining / quota_used stored and forwarded to ProviderHealth
  - init_health_monitor() / get_health_monitor() singleton pattern
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest

from providers.base import FailureType, ProviderStatus
from providers.health_monitor import (
    ProviderHealthMonitor,
    get_health_monitor,
    init_health_monitor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_monitor() -> ProviderHealthMonitor:
    return ProviderHealthMonitor()


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_creates_entry_as_disabled(self):
        mon = _make_monitor()
        mon.register("TestProv", disabled=True)
        h = mon.get_health("TestProv")
        assert h.status == ProviderStatus.DISABLED

    def test_register_default_not_disabled(self):
        mon = _make_monitor()
        mon.register("TestProv")
        h = mon.get_health("TestProv")
        # Registered with no failures and no disabled → OK
        assert h.status == ProviderStatus.OK

    def test_register_idempotent(self):
        """Calling register twice does not reset existing state."""
        mon = _make_monitor()
        mon.register("TestProv")
        mon.record_failure("TestProv", "oops", FailureType.HTTP_ERROR)
        mon.register("TestProv")  # second call — should not reset
        h = mon.get_health("TestProv")
        assert h.consecutive_failures == 1


# ---------------------------------------------------------------------------
# record_success()
# ---------------------------------------------------------------------------

class TestRecordSuccess:
    def test_success_sets_ok_status(self):
        mon = _make_monitor()
        mon.record_success("PP")
        assert mon.get_health("PP").status == ProviderStatus.OK

    def test_success_resets_consecutive_failures(self):
        mon = _make_monitor()
        for _ in range(5):
            mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        mon.record_success("PP")
        assert mon.get_health("PP").consecutive_failures == 0

    def test_success_clears_failure_type(self):
        mon = _make_monitor()
        mon.record_failure("PP", "err", FailureType.QUOTA)
        mon.record_success("PP")
        assert mon.get_health("PP").failure_type is None

    def test_success_stores_quota_remaining(self):
        mon = _make_monitor()
        mon.record_success("OddsAPI", quota_remaining=450, quota_used=50)
        h = mon.get_health("OddsAPI")
        assert h.quota_remaining == 450
        assert h.quota_used == 50

    def test_success_partial_quota_update(self):
        """Only the provided quota fields should be updated."""
        mon = _make_monitor()
        mon.record_success("OddsAPI", quota_remaining=400, quota_used=100)
        mon.record_success("OddsAPI", quota_remaining=350)  # quota_used not passed
        h = mon.get_health("OddsAPI")
        assert h.quota_remaining == 350
        assert h.quota_used == 100  # unchanged

    def test_success_sets_last_success_timestamp(self):
        from datetime import datetime
        mon = _make_monitor()
        before = datetime.utcnow()
        mon.record_success("PP")
        after  = datetime.utcnow()
        h = mon.get_health("PP")
        assert h.last_success is not None
        assert before <= h.last_success <= after


# ---------------------------------------------------------------------------
# record_failure()
# ---------------------------------------------------------------------------

class TestRecordFailure:
    def test_failure_increments_counter(self):
        mon = _make_monitor()
        mon.record_failure("PP", "oops", FailureType.HTTP_ERROR)
        assert mon.get_health("PP").consecutive_failures == 1

    def test_multiple_failures_accumulate(self):
        mon = _make_monitor()
        for _ in range(4):
            mon.record_failure("PP", "oops", FailureType.HTTP_ERROR)
        assert mon.get_health("PP").consecutive_failures == 4

    def test_failure_stores_failure_type(self):
        mon = _make_monitor()
        mon.record_failure("PP", "403", FailureType.BLOCKED)
        assert mon.get_health("PP").failure_type == FailureType.BLOCKED

    def test_failure_stores_error_message(self):
        mon = _make_monitor()
        mon.record_failure("PP", "HTTP 403 forbidden", FailureType.HTTP_ERROR)
        assert "403" in mon.get_health("PP").error_msg

    def test_error_message_capped_at_200_chars(self):
        mon = _make_monitor()
        long_msg = "x" * 500
        mon.record_failure("PP", long_msg, FailureType.UNKNOWN)
        assert len(mon.get_health("PP").error_msg) <= 200

    def test_failure_sets_last_failure_timestamp(self):
        from datetime import datetime
        mon = _make_monitor()
        before = datetime.utcnow()
        mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        after  = datetime.utcnow()
        h = mon.get_health("PP")
        assert h.last_failure is not None
        assert before <= h.last_failure <= after


# ---------------------------------------------------------------------------
# Status derivation rules
# ---------------------------------------------------------------------------

class TestStatusDerivation:
    def test_zero_failures_is_ok(self):
        mon = _make_monitor()
        mon.register("PP")
        assert mon.get_health("PP").status == ProviderStatus.OK

    def test_one_failure_is_degraded(self):
        mon = _make_monitor()
        mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        assert mon.get_health("PP").status == ProviderStatus.DEGRADED

    def test_two_failures_is_degraded(self):
        mon = _make_monitor()
        for _ in range(2):
            mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        assert mon.get_health("PP").status == ProviderStatus.DEGRADED

    def test_three_failures_is_down(self):
        mon = _make_monitor()
        for _ in range(3):
            mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        assert mon.get_health("PP").status == ProviderStatus.DOWN

    def test_many_failures_is_still_down(self):
        mon = _make_monitor()
        for _ in range(10):
            mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        assert mon.get_health("PP").status == ProviderStatus.DOWN

    def test_quota_failure_overrides_to_quota_exhausted(self):
        """QUOTA should take priority over DOWN threshold check."""
        mon = _make_monitor()
        mon.record_failure("OddsAPI", "quota", FailureType.QUOTA)
        assert mon.get_health("OddsAPI").status == ProviderStatus.QUOTA_EXHAUSTED

    def test_quota_exhausted_on_single_failure(self):
        mon = _make_monitor()
        mon.record_failure("OddsAPI", "quota", FailureType.QUOTA)
        h = mon.get_health("OddsAPI")
        assert h.consecutive_failures == 1
        assert h.status == ProviderStatus.QUOTA_EXHAUSTED

    def test_blocked_overrides_regardless_of_count(self):
        mon = _make_monitor()
        mon.record_failure("PP", "403", FailureType.BLOCKED)
        assert mon.get_health("PP").status == ProviderStatus.BLOCKED

    def test_success_after_quota_clears_quota_status(self):
        mon = _make_monitor()
        mon.record_failure("OddsAPI", "quota", FailureType.QUOTA)
        mon.record_success("OddsAPI")
        assert mon.get_health("OddsAPI").status == ProviderStatus.OK

    def test_success_after_blocked_clears_blocked_status(self):
        mon = _make_monitor()
        mon.record_failure("PP", "403", FailureType.BLOCKED)
        mon.record_success("PP")
        assert mon.get_health("PP").status == ProviderStatus.OK

    def test_disabled_provider_shows_disabled(self):
        mon = _make_monitor()
        mon.register("PP", disabled=True)
        assert mon.get_health("PP").status == ProviderStatus.DISABLED


# ---------------------------------------------------------------------------
# is_* convenience helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_is_quota_exhausted_true(self):
        mon = _make_monitor()
        mon.record_failure("OddsAPI", "quota", FailureType.QUOTA)
        assert mon.is_quota_exhausted("OddsAPI") is True

    def test_is_quota_exhausted_false_for_other_errors(self):
        mon = _make_monitor()
        mon.record_failure("OddsAPI", "http error", FailureType.HTTP_ERROR)
        assert mon.is_quota_exhausted("OddsAPI") is False

    def test_is_quota_exhausted_false_for_unknown_provider(self):
        mon = _make_monitor()
        assert mon.is_quota_exhausted("NoSuchProvider") is False

    def test_is_blocked_true(self):
        mon = _make_monitor()
        mon.record_failure("PP", "403", FailureType.BLOCKED)
        assert mon.is_blocked("PP") is True

    def test_is_blocked_false_for_http_error(self):
        mon = _make_monitor()
        mon.record_failure("PP", "500", FailureType.HTTP_ERROR)
        assert mon.is_blocked("PP") is False

    def test_is_blocked_false_for_unknown_provider(self):
        mon = _make_monitor()
        assert mon.is_blocked("NoSuchProvider") is False

    def test_is_healthy_true_when_ok(self):
        mon = _make_monitor()
        mon.record_success("PP")
        assert mon.is_healthy("PP") is True

    def test_is_healthy_true_when_degraded(self):
        mon = _make_monitor()
        mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        # 1 failure = DEGRADED, still considered healthy for polling purposes
        assert mon.is_healthy("PP") is True

    def test_is_healthy_false_when_down(self):
        mon = _make_monitor()
        for _ in range(3):
            mon.record_failure("PP", "err", FailureType.HTTP_ERROR)
        assert mon.is_healthy("PP") is False

    def test_is_healthy_false_when_quota_exhausted(self):
        mon = _make_monitor()
        mon.record_failure("OddsAPI", "quota", FailureType.QUOTA)
        assert mon.is_healthy("OddsAPI") is False

    def test_is_healthy_true_for_unknown_provider(self):
        """Unknown providers are treated as healthy (fail-open)."""
        mon = _make_monitor()
        assert mon.is_healthy("UnknownProvider") is True


# ---------------------------------------------------------------------------
# get_all_health()
# ---------------------------------------------------------------------------

class TestGetAllHealth:
    def test_returns_all_registered_providers(self):
        mon = _make_monitor()
        mon.register("PP")
        mon.register("OddsAPI")
        mon.register("Underdog")
        all_h = mon.get_all_health()
        assert set(all_h.keys()) == {"PP", "OddsAPI", "Underdog"}

    def test_returns_empty_dict_when_nothing_registered(self):
        mon = _make_monitor()
        assert mon.get_all_health() == {}

    def test_auto_registered_providers_appear(self):
        """record_failure on an unregistered name auto-registers it."""
        mon = _make_monitor()
        mon.record_failure("NewProv", "err", FailureType.HTTP_ERROR)
        assert "NewProv" in mon.get_all_health()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_init_returns_monitor(self):
        mon = init_health_monitor()
        assert isinstance(mon, ProviderHealthMonitor)

    def test_get_after_init_returns_same_instance(self):
        mon = init_health_monitor()
        assert get_health_monitor() is mon

    def test_get_before_init_returns_none_or_previous(self):
        """get_health_monitor() should not crash when called before init."""
        result = get_health_monitor()
        # May be None or the previously-init'd singleton — just shouldn't raise
        assert result is None or isinstance(result, ProviderHealthMonitor)


# ---------------------------------------------------------------------------
# ProviderHealth display helpers
# ---------------------------------------------------------------------------

class TestProviderHealthDisplay:
    def test_status_emoji_ok(self):
        mon = _make_monitor()
        mon.record_success("PP")
        h = mon.get_health("PP")
        assert h.status_emoji == "🟢"

    def test_status_emoji_quota(self):
        mon = _make_monitor()
        mon.record_failure("PP", "quota", FailureType.QUOTA)
        h = mon.get_health("PP")
        assert h.status_emoji == "⛔"

    def test_status_emoji_blocked(self):
        mon = _make_monitor()
        mon.record_failure("PP", "403", FailureType.BLOCKED)
        h = mon.get_health("PP")
        assert h.status_emoji == "🚫"

    def test_format_last_success_never(self):
        mon = _make_monitor()
        mon.register("PP")
        h = mon.get_health("PP")
        assert h.format_last_success() == "never"

    def test_format_last_success_recent(self):
        from datetime import datetime, timedelta
        mon = _make_monitor()
        mon.record_success("PP")
        h = mon.get_health("PP")
        age_str = h.format_last_success()
        # Should be "Xs ago" for a very recent success
        assert "ago" in age_str
