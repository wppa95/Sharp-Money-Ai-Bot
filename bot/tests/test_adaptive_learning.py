from __future__ import annotations

import pytest

from database import Database, PropOpportunityLog
from engine.adaptive_learning import (
    adaptive_score_adjustment,
    build_adaptive_weight,
    sample_from_outcomes,
)
from engine.ranking import compute_ranking


def test_learning_eligibility_excludes_non_model_misses_and_pushes():
    sample = sample_from_outcomes(
        provider="DraftKings",
        sport="NBA",
        market_type="Points",
        outcomes=[
            ("HIT", None),
            ("MISS", "Model"),
            ("MISS", "Variance"),
            ("MISS", "Market"),
            ("PUSH", None),
        ],
    )
    assert sample.eligible_samples == 2
    assert sample.model_errors == 1
    assert sample.excluded_misses == 2
    assert sample.pushes == 1
    assert sample.total_resolved == 5


def test_learning_minimum_sample_blocks_weight():
    sample = sample_from_outcomes(
        provider="Underdog",
        sport="MLB",
        market_type="Hits",
        outcomes=[("HIT", None)] * 29 + [("MISS", "Model")] * 5,
    )
    weight = build_adaptive_weight(
        sample,
        version="test-v1",
        min_sample_size=35,
        min_model_errors=5,
    )
    assert weight.status == "INSUFFICIENT_SAMPLE"
    assert weight.multiplier == 1.0


def test_adaptive_adjustment_is_bounded_and_ranking_only():
    assert adaptive_score_adjustment(80, 0.9) == -2
    assert adaptive_score_adjustment(80, 1.1) == 2
    assert adaptive_score_adjustment(80, 5.0) == 2
    result = compute_ranking(
        steam_score=75,
        ev_edge_pct=5.0,
        fair_probability=0.55,
        n_books_moving=3,
        sharp_book_count=1,
        market_agreement=0.85,
        movement_speed=1.0,
        liquidity_score=60,
        minutes_to_game=360,
        adaptive_multiplier=1.1,
        adaptive_weight_version="test-v1",
    )
    assert result.historical_breakdown.adaptive_adj == 2
    assert result.adaptive_weight_version == "test-v1"
    # The adaptive value changes ranking metadata only; it cannot alter the
    # confidence result or introduce a delivery-specific field.
    assert result.confidence_result.confidence_score == result.base_score


@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_adaptive_rebuild_is_provider_sport_market_scoped_and_audited(db):
    rows = []
    for index in range(30):
        rows.append(
            PropOpportunityLog(
                external_id=f"dk-{index}",
                player_name=f"Player {index}",
                team="TEAM",
                sport="NBA",
                stat_type="Points",
                line_value=20.5,
                recommendation="OVER",
                decision_tier="A",
                confidence=80,
                provider="DraftKings",
                result="HIT" if index < 25 else "MISS",
                grade_result_source="external_player_stats",
                error_type=None if index < 25 else "Model",
            )
        )
    async with db.session() as session:
        session.add_all(rows)
        await session.commit()

    descriptive = await db.rebuild_adaptive_learning_weights(
        min_sample_size=30,
        min_model_errors=5,
        activate=False,
    )
    assert descriptive["active"] is False
    assert descriptive["eligible_groups"] == 1

    status = await db.get_adaptive_learning_status()
    assert status["active"] is False
    assert status["total_groups"] == 1
    assert status["weights"][0]["provider"] == "DraftKings"
    assert status["weights"][0]["sport"] == "NBA"
    assert status["weights"][0]["market_type"] == "Points"
    assert await db.get_active_adaptive_weight("DraftKings", "NBA", "Points") is None

    active = await db.rebuild_adaptive_learning_weights(
        min_sample_size=30,
        min_model_errors=5,
        activate=True,
    )
    assert active["active"] is True
    weight = await db.get_active_adaptive_weight("DraftKings", "NBA", "Points")
    assert weight is not None
    assert weight["version"] == active["version"]
    assert await db.get_active_adaptive_weight("Underdog", "NBA", "Points") is None