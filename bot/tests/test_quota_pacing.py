"""Regression tests for strict, restart-safe OddsAPI V4 pacing."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from providers.quota_pacing import OddsApiQuotaPacer, parse_deadline


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def _make_pacer(tmp_path, clock, **kwargs) -> OddsApiQuotaPacer:
    return OddsApiQuotaPacer(
        state_path=str(tmp_path / "odds_api_pacing.json"),
        now_fn=clock,
        timezone_name="America/Chicago",
        **kwargs,
    )


def test_deadline_parser_remains_compatible():
    assert parse_deadline("2026-08-31T00:00:00Z").tzinfo == timezone.utc
    assert parse_deadline("2026-08-31T00:00:00").tzinfo == timezone.utc


def test_v4_defaults_are_hard_daily_policy(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)

    assert pacer.daily_limit == 16
    assert pacer.min_interval_seconds == 60 * 60
    assert pacer.reserve_request_slot() is True
    assert pacer.daily_count == 1


def test_16_day_hard_cap_blocks_17th_request(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)

    for _ in range(16):
        assert pacer.reserve_request_slot() is True
        clock.advance(hours=1)

    assert pacer.reserve_request_slot() is False
    assert pacer.daily_count == 16
    assert pacer.block_reason() == "daily limit reached"
    assert pacer.telemetry().status == "DAILY_LIMIT"


def test_no_request_before_one_hour(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)

    assert pacer.reserve_request_slot() is True
    clock.advance(minutes=59, seconds=59)

    assert pacer.can_request() is False
    assert pacer.reserve_request_slot() is False
    assert "minimum interval" in pacer.block_reason()


@pytest.mark.parametrize("extra_seconds", [0, 1, 60])
def test_request_allowed_at_or_after_one_hour(tmp_path, extra_seconds):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)

    assert pacer.reserve_request_slot() is True
    clock.advance(hours=1, seconds=extra_seconds)

    assert pacer.reserve_request_slot() is True
    assert pacer.daily_count == 2


def test_daily_counter_resets_at_configured_timezone_boundary(tmp_path):
    # 04:59 UTC is 23:59 CDT on August 31; 05:00 UTC is midnight September 1.
    clock = FakeClock(datetime(2026, 9, 1, 4, 59, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)
    assert pacer.reserve_request_slot() is True
    assert pacer.daily_count == 1

    clock.advance(minutes=1)

    assert pacer.daily_count == 0
    assert pacer.reserve_request_slot() is True
    assert pacer.daily_count == 1


def test_restart_does_not_reset_daily_count_or_cooldown(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    first = _make_pacer(tmp_path, clock)
    assert first.reserve_request_slot() is True

    restarted = _make_pacer(tmp_path, clock)
    assert restarted.daily_count == 1
    assert restarted.can_request() is False
    assert restarted.block_reason().startswith("minimum interval")

    clock.advance(hours=1)
    assert restarted.reserve_request_slot() is True
    assert restarted.daily_count == 2

    with open(tmp_path / "odds_api_pacing.json") as fh:
        state = json.load(fh)
    assert set(state) == {"date", "daily_count", "last_request_at"}


def test_daily_limit_wins_even_when_interval_has_elapsed(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock, daily_limit=1)
    assert pacer.reserve_request_slot() is True
    clock.advance(hours=1)

    assert pacer.reserve_request_slot() is False
    assert pacer.daily_count == 1


def test_diagnostics_include_required_pacing_fields(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)
    summary = pacer.telemetry().summary

    assert "Daily usage: 0/16" in summary
    assert "Last request: never" in summary
    assert "Next eligible request: now" in summary
    assert "Daily limit: 16" in summary
    assert "Minimum interval: 60m" in summary


def test_provider_quota_exhaustion_remains_a_safe_block(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)
    pacer.update_quota(remaining=0, used=500)

    assert pacer.can_request() is False
    assert pacer.telemetry().status == "EXHAUSTED"


def test_invalid_market_does_not_add_adaptive_backoff(tmp_path):
    clock = FakeClock(datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    pacer = _make_pacer(tmp_path, clock)
    pacer.update_quota(remaining=100, used=400)
    pacer.record_request_failed(0.0, status=422)

    assert pacer.telemetry().backoff_seconds == 0