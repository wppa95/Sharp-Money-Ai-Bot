"""Focused tests for adaptive OddsAPI quota pacing."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from providers.quota_pacing import OddsApiQuotaPacer, parse_deadline


def test_deadline_parser_accepts_z_and_naive_values():
    assert parse_deadline("2026-08-31T00:00:00Z").tzinfo == timezone.utc
    assert parse_deadline("2026-08-31T00:00:00").tzinfo == timezone.utc


def test_required_rate_uses_remaining_quota_and_live_deadline():
    pacer = OddsApiQuotaPacer(
        quota_limit=30_000,
        deadline="2026-08-31T00:00:00Z",
    )
    pacer.update_quota(remaining=28_700, used=1_300)
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    assert pacer.hours_remaining(now) == pytest.approx(84.0)
    assert pacer.required_calls_per_hour() == pytest.approx(28_700 / pacer.hours_remaining(), rel=0.01)
    assert pacer.telemetry().required_calls_per_minute > 5.0


def test_required_rate_learns_provider_credit_cost():
    pacer = OddsApiQuotaPacer(deadline="2026-08-31T00:00:00Z")
    pacer.update_quota(remaining=28_700, used=1_300)
    pacer.update_quota(remaining=28_698, used=1_302)
    assert pacer.telemetry().quota_cost_per_request == 2
    assert pacer.required_calls_per_hour() == pytest.approx(
        28_698 / 2 / pacer.hours_remaining(), rel=0.01
    )


def test_unknown_quota_waits_for_headers_without_claiming_pace():
    pacer = OddsApiQuotaPacer(deadline="2026-08-31T00:00:00Z")
    telemetry = pacer.telemetry()
    assert telemetry.status == "WAITING_FOR_QUOTA"
    assert telemetry.required_calls_per_hour == 0


@pytest.mark.asyncio
async def test_backoff_blocks_until_provider_retry_window():
    pacer = OddsApiQuotaPacer(deadline="2026-08-31T00:00:00Z")
    pacer.update_quota(remaining=100)
    pacer.record_error(429)
    assert pacer.can_request() is False
    with patch("providers.quota_pacing.asyncio.sleep", new=asyncio.sleep):
        # Avoid waiting in the test; a second 429 is still bounded and visible.
        assert pacer.telemetry().backoff_seconds > 0


def test_zero_remaining_quota_is_exhausted():
    pacer = OddsApiQuotaPacer(deadline="2026-08-31T00:00:00Z")
    pacer.update_quota(remaining=0, used=30_000)
    assert pacer.can_request() is False
    assert pacer.telemetry().status == "EXHAUSTED"


def test_invalid_market_does_not_backoff_useful_poller():
    """Permanent 422 request-shape errors must not delay valid market calls."""
    pacer = OddsApiQuotaPacer(deadline="2026-08-31T00:00:00Z")
    pacer.update_quota(remaining=100, used=29_900)
    pacer.record_request_failed(0.0, status=422)
    assert pacer.can_request() is True
    assert pacer.telemetry().backoff_seconds == 0