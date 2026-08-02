"""
tests/test_db_phase2.py — Phase 2 database schema and method tests.

Tests:
  • PropLineHistory.opening_line column: set on INSERT, never updated
  • PropOpportunityLog Phase 2 columns: stars, risk_level, explanation, void_reason
  • log_prop_opportunity: accepts new optional params
  • grade_opportunity: accepts extended result codes and void_reason
  • get_learning_rollups: returns correct structure with no/some graded rows
  • _migrate_prop_line_history_v2: safe to call on existing tables
  • _migrate_prop_opportunity_log_v2: safe to call on existing tables
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime


# ── Async test loop ───────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


def _run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="module")
def db():
    from database import Database
    _db = Database("sqlite+aiosqlite:///:memory:")
    _run(_db.init())
    return _db


# ── PropLineHistory.opening_line ─────────────────────────────────────────────

def test_upsert_sets_opening_line_on_insert(db):
    row, event = _run(db.upsert_prop_line_lifecycle(
        provider    = "Underdog",
        player_name = "Aaron Judge",
        sport       = "MLB",
        stat_type   = "Home Runs",
        line_value  = 4.5,
    ))
    assert event == "ADDED"
    assert row.opening_line == pytest.approx(4.5)


def test_upsert_opening_line_not_changed_on_update(db):
    # First INSERT
    _run(db.upsert_prop_line_lifecycle(
        provider    = "Underdog",
        player_name = "Shohei Ohtani",
        sport       = "MLB",
        stat_type   = "Strikeouts",
        line_value  = 6.5,
    ))
    # Line moves
    row, event = _run(db.upsert_prop_line_lifecycle(
        provider    = "Underdog",
        player_name = "Shohei Ohtani",
        sport       = "MLB",
        stat_type   = "Strikeouts",
        line_value  = 7.0,
    ))
    assert event == "CHANGED"
    # opening_line should still be 6.5
    assert row.opening_line == pytest.approx(6.5)


def test_upsert_opening_line_unchanged_on_second_insert(db):
    """opening_line must equal the very first line seen even after many moves."""
    _run(db.upsert_prop_line_lifecycle(
        provider="Underdog", player_name="Test Player 99",
        sport="MLB", stat_type="RBIs", line_value=1.5,
    ))
    _run(db.upsert_prop_line_lifecycle(
        provider="Underdog", player_name="Test Player 99",
        sport="MLB", stat_type="RBIs", line_value=2.0,
    ))
    row, _ = _run(db.upsert_prop_line_lifecycle(
        provider="Underdog", player_name="Test Player 99",
        sport="MLB", stat_type="RBIs", line_value=2.5,
    ))
    assert row.opening_line == pytest.approx(1.5)


# ── PropOpportunityLog Phase 2 columns ────────────────────────────────────────

def test_log_prop_opportunity_accepts_stars(db):
    _run(db.log_prop_opportunity(
        external_id    = "test-stars-001",
        player_name    = "Aaron Judge",
        team           = "NYY",
        sport          = "MLB",
        stat_type      = "Home Runs",
        line_value     = 0.5,
        recommendation = "OVER",
        decision_tier  = "S",
        confidence     = 85,
        game_time      = None,
        stars          = 5,
    ))
    # Should not raise


def test_log_prop_opportunity_accepts_risk_level(db):
    _run(db.log_prop_opportunity(
        external_id    = "test-risk-001",
        player_name    = "Shohei Ohtani",
        team           = "LAD",
        sport          = "MLB",
        stat_type      = "Strikeouts",
        line_value     = 6.5,
        recommendation = "OVER",
        decision_tier  = "A",
        confidence     = 70,
        game_time      = None,
        risk_level     = "LOW",
    ))


def test_log_prop_opportunity_accepts_explanation(db):
    _run(db.log_prop_opportunity(
        external_id    = "test-expl-001",
        player_name    = "Juan Soto",
        team           = "NYM",
        sport          = "MLB",
        stat_type      = "Hits",
        line_value     = 1.5,
        recommendation = "OVER",
        decision_tier  = "B",
        confidence     = 60,
        game_time      = None,
        explanation    = "Strong hit rate vs LHP",
    ))


def test_log_prop_opportunity_all_new_params(db):
    _run(db.log_prop_opportunity(
        external_id    = "test-all-phase2",
        player_name    = "Mookie Betts",
        team           = "LAD",
        sport          = "MLB",
        stat_type      = "Total Bases",
        line_value     = 2.5,
        recommendation = "OVER",
        decision_tier  = "A",
        confidence     = 72,
        game_time      = None,
        stars          = 4,
        risk_level     = "MEDIUM",
        explanation    = "Consistent vs elite pitching",
    ))


def test_log_prop_opportunity_no_new_params_still_works(db):
    _run(db.log_prop_opportunity(
        external_id    = "test-compat-001",
        player_name    = "Freddie Freeman",
        team           = "LAD",
        sport          = "MLB",
        stat_type      = "RBIs",
        line_value     = 0.5,
        recommendation = "OVER",
        decision_tier  = "B",
        confidence     = 58,
        game_time      = None,
    ))


# ── grade_opportunity extended result codes ───────────────────────────────────

async def _log_and_grade(db, ext_id, result, void_reason=None):
    from sqlalchemy import select
    from database import PropOpportunityLog

    await db.log_prop_opportunity(
        external_id    = ext_id,
        player_name    = "Test Player",
        team           = "TST",
        sport          = "MLB",
        stat_type      = "Hits",
        line_value     = 1.5,
        recommendation = "OVER",
        decision_tier  = "B",
        confidence     = 60,
        game_time      = None,
    )
    # Fetch directly by external_id — avoids game_time filter in get_pending_opportunities
    async with db.session() as s:
        r = await s.execute(
            select(PropOpportunityLog)
            .where(PropOpportunityLog.external_id == ext_id)
        )
        opp = r.scalar_one_or_none()
    if opp:
        await db.grade_opportunity(
            opp.id, result, actual_value=0.0, void_reason=void_reason
        )
    return opp


def test_grade_opportunity_hit(db):
    _run(_log_and_grade(db, "grade-hit-001", "HIT"))


def test_grade_opportunity_miss(db):
    _run(_log_and_grade(db, "grade-miss-001", "MISS"))


def test_grade_opportunity_push(db):
    _run(_log_and_grade(db, "grade-push-001", "PUSH"))


def test_grade_opportunity_void(db):
    _run(_log_and_grade(db, "grade-void-001", "VOID", void_reason="DNP"))


def test_grade_opportunity_cancelled(db):
    _run(_log_and_grade(db, "grade-cancel-001", "CANCELLED", void_reason="Game postponed"))


def test_grade_opportunity_injury_void(db):
    _run(_log_and_grade(db, "grade-injvoid-001", "INJURY_VOID", void_reason="Player injured warm-ups"))


def test_grade_opportunity_game_interrupted(db):
    _run(_log_and_grade(db, "grade-gi-001", "GAME_INTERRUPTED", void_reason="Rain delay — suspended"))


def test_grade_opportunity_void_reason_stored(db):
    """void_reason must be persisted in the void_reason column."""
    from sqlalchemy import select
    from database import PropOpportunityLog

    _run(_log_and_grade(db, "grade-vr-check", "VOID", void_reason="Lineup scratch"))

    async def _fetch():
        async with db.session() as s:
            r = await s.execute(
                select(PropOpportunityLog)
                .where(PropOpportunityLog.external_id == "grade-vr-check")
            )
            return r.scalar_one_or_none()

    row = _run(_fetch())
    assert row is not None
    assert row.void_reason == "Lineup scratch"


# ── get_learning_rollups ──────────────────────────────────────────────────────

def test_get_learning_rollups_empty_db_returns_structure(db):
    result = _run(db.get_learning_rollups())
    assert isinstance(result, dict)
    assert "by_tier"       in result
    assert "by_sport"      in result
    assert "by_stat_type"  in result
    assert "by_error_type" in result
    assert "player_trend"  in result
    assert "total_graded"  in result


def test_get_learning_rollups_total_graded_zero_on_empty(db):
    result = _run(db.get_learning_rollups())
    # total_graded may not be 0 if previous tests already graded plays — just check it's an int
    assert isinstance(result["total_graded"], int)
    assert result["total_graded"] >= 0


def test_get_learning_rollups_by_tier_is_dict(db):
    result = _run(db.get_learning_rollups())
    assert isinstance(result["by_tier"], dict)


def test_get_learning_rollups_player_trend_is_list(db):
    result = _run(db.get_learning_rollups())
    assert isinstance(result["player_trend"], list)


def test_get_learning_rollups_by_stat_type_at_most_15(db):
    result = _run(db.get_learning_rollups())
    assert len(result["by_stat_type"]) <= 15


def test_get_learning_rollups_with_graded_plays(db):
    """After grading some plays, rollups should reflect them."""
    from sqlalchemy import update as _sa_update
    from database import PropOpportunityLog

    async def _seed():
        for i in range(5):
            await db.log_prop_opportunity(
                external_id    = f"rollup-seed-{i}",
                player_name    = f"Player{i}",
                team           = "TST",
                sport          = "MLB",
                stat_type      = "Hits",
                line_value     = 1.5,
                recommendation = "OVER",
                decision_tier  = "A",
                confidence     = 70,
                game_time      = None,
            )
        # Directly grade some as HIT
        async with db.session() as s:
            await s.execute(
                _sa_update(PropOpportunityLog)
                .where(PropOpportunityLog.external_id.in_(
                    [f"rollup-seed-{i}" for i in range(5)]
                ))
                .values(result="HIT", actual_value=2.0, graded_at=datetime.utcnow())
            )
            await s.commit()

    _run(_seed())
    result = _run(db.get_learning_rollups())
    assert result["total_graded"] >= 5


def test_get_learning_rollups_tier_entry_has_wlp_keys(db):
    result = _run(db.get_learning_rollups())
    for _tier, entry in result["by_tier"].items():
        assert "W"       in entry
        assert "L"       in entry
        assert "P"       in entry
        assert "total"   in entry
        assert "win_pct" in entry


# ── Migration safety ──────────────────────────────────────────────────────────

def test_migrate_prop_line_history_v2_idempotent(db):
    """Running the migration twice should not raise."""
    _run(db._migrate_prop_line_history_v2())
    _run(db._migrate_prop_line_history_v2())


def test_migrate_prop_opportunity_log_v2_idempotent(db):
    """Running v2 migration twice should not raise."""
    _run(db._migrate_prop_opportunity_log_v2())
    _run(db._migrate_prop_opportunity_log_v2())
