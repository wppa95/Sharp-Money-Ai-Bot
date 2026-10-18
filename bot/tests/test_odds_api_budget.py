from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from providers.odds_api_budget import (
    ODDS_API_MONTHLY_HARD_LIMIT,
    ODDS_API_MONTHLY_TARGET,
    OddsApiCreditBudget,
)

UTC = timezone.utc


def _seed(path, now: datetime, *, used: int = 0, next_at: datetime | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    period_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    path.write_text(
        json.dumps({
            "version": 2,
            "period_start": period_start.isoformat(),
            "credits_used": used,
            "reported_used": 0,
            "reported_remaining": None,
            "next_paid_request_at": (next_at or now).isoformat(),
            "rotation_cursor": 0,
            "reservations": {},
            "caches": {},
        }),
        encoding="utf-8",
    )


def test_user_confirmed_target_and_hard_limit():
    assert ODDS_API_MONTHLY_TARGET == 450
    assert ODDS_API_MONTHLY_HARD_LIMIT == 500


def test_first_paid_request_is_persisted_in_future_and_shared_between_instances(tmp_path):
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    path = tmp_path / "shared.json"
    _seed(path, now, next_at=now - timedelta(seconds=1))
    first = OddsApiCreditBudget(str(path))
    second = OddsApiCreditBudget(str(path))

    next_at = first.initialize_schedule(now)
    assert next_at > now
    reservation, reason = first.reserve(
        endpoint="/sports/nba/scores",
        params={"daysFrom": 1},
        max_cost=2,
        now=next_at,
    )
    assert reservation and not reason
    other_reservation, other_reason = second.reserve(
        endpoint="/sports/nfl/events/event-1/markets",
        params={"regions": "us"},
        max_cost=1,
        now=next_at,
    )
    assert other_reservation is None
    assert other_reason == "paced"


def test_all_endpoints_share_persistent_credit_total_and_header_reconciliation(tmp_path):
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    path = tmp_path / "shared.json"
    _seed(path, now, next_at=now - timedelta(seconds=1))
    budget = OddsApiCreditBudget(str(path))
    first, reason = budget.reserve(
        endpoint="/sports/nba/events/event-1/markets",
        params={"regions": "us"},
        max_cost=1,
        now=now,
    )
    assert first and not reason
    budget.complete(
        first,
        actual_cost=1,
        outcome="success",
        now=now,
        reported_used=3,
        reported_remaining=497,
    )
    assert budget.diagnostics(now)["credits_used"] == 3

    restarted = OddsApiCreditBudget(str(path))
    second, reason = restarted.reserve(
        endpoint="/sports/nfl/scores",
        params={},
        max_cost=1,
        now=now + timedelta(days=1),
    )
    assert second and not reason
    restarted.complete(
        second,
        actual_cost=1,
        outcome="success",
        now=now + timedelta(days=1),
        reported_used=4,
    )
    assert OddsApiCreditBudget(str(path)).diagnostics(
        now + timedelta(days=1)
    )["credits_used"] == 4


def test_unknown_interrupted_reservation_charges_maximum_after_restart(tmp_path):
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    path = tmp_path / "shared.json"
    _seed(path, now, next_at=now - timedelta(seconds=1))
    budget = OddsApiCreditBudget(str(path))
    reservation, _ = budget.reserve(
        endpoint="/sports/nba/events/event-1/odds",
        params={"markets": "player_points,player_rebounds"},
        max_cost=2,
        now=now,
    )
    assert reservation

    restarted = OddsApiCreditBudget(str(path))
    reservation, reason = restarted.reserve(
        endpoint="/sports/nba/scores",
        params={},
        max_cost=1,
        now=now + timedelta(minutes=4),
    )
    assert reservation is None
    assert reason == "paced"
    after_interruption = restarted.diagnostics(now + timedelta(minutes=4))
    assert after_interruption["credits_used"] == 2
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["last_request"]["outcome"] == "unknown_after_interruption"


def test_hard_limit_and_unknown_ledger_fail_closed(tmp_path):
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    path = tmp_path / "at-cap.json"
    _seed(path, now, used=ODDS_API_MONTHLY_HARD_LIMIT, next_at=now)
    budget = OddsApiCreditBudget(str(path))
    reservation, reason = budget.reserve(
        endpoint="/sports/nba/scores", params={}, max_cost=1, now=now
    )
    assert reservation is None
    assert reason in {"hard_limit_reached", "working_target_reached"}

    broken_path = tmp_path / "broken.json"
    broken_path.write_text("{not json", encoding="utf-8")
    broken = OddsApiCreditBudget(str(broken_path))
    state = broken.diagnostics(now)
    assert state["budget_state_unknown"] is True
    assert state["credits_used"] >= ODDS_API_MONTHLY_HARD_LIMIT


def test_budget_rolls_only_at_utc_month_boundary_and_keeps_history(tmp_path):
    october = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    november = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    path = tmp_path / "month.json"
    _seed(path, october, used=123, next_at=october)
    budget = OddsApiCreditBudget(str(path))
    assert budget.diagnostics(october)["credits_used"] == 123
    after_reset = budget.diagnostics(november)
    assert after_reset["credits_used"] == 0
    budget.initialize_schedule(november)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["period_start"] == datetime(2026, 11, 1, tzinfo=UTC).isoformat()
    assert state["period_history"][
        datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    ]["credits_used"] == 123
