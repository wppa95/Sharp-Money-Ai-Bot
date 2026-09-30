"""Contracts for the isolated, evidence-bound AI analyst layer."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from database import Database, PropOpportunityLog
from engine.ai_analyst import (
    AnalystDecision,
    AnalystInput,
    EvidenceBoundAnalyst,
    build_advisory_from_pipeline,
    analyst_grade,
)


def test_missing_direction_is_explicitly_unavailable():
    result = EvidenceBoundAnalyst().analyze(
        AnalystInput(
            player_name="A. Player",
            sport="NBA",
            stat_type="Points",
            line=21.5,
            provider="Underdog",
            event_id="event-1",
            quantitative={},
            evidence={},
        )
    )
    assert result.prediction == "UNAVAILABLE"
    assert result.evidence_status == "UNAVAILABLE"
    assert "21.5" not in result.rationale
    assert "direction" in result.missing_inputs


def test_analyst_never_invents_missing_values():
    result = build_advisory_from_pipeline(
        player_name="A. Player",
        sport="NHL",
        stat_type="Shots on Goal",
        line=3.5,
        provider="Underdog",
        decision=type("Decision", (), {
            "recommendation": "OVER",
            "confidence": 78,
            "evidence": {},
        })(),
    )
    assert result.prediction == "OVER"
    assert result.evidence_status == "PARTIAL"
    assert "supporting_history" in result.missing_inputs
    assert "market_evidence" in result.missing_inputs
    assert "3.5" not in result.rationale


def test_provider_contract_is_model_agnostic():
    class FakeProvider:
        model = "test-model"
        model_version = "test-1"

        def analyze(self, snapshot):
            assert snapshot.sport == "MLB"
            return AnalystDecision(
                prediction="UNDER",
                evidence_status="AVAILABLE",
                rationale="provider result",
                evidence_used=("stored_input",),
                missing_inputs=(),
                confidence=61,
                confidence_source="test",
                risk_level="LOW",
                model=self.model,
                model_version=self.model_version,
            )

    result = build_advisory_from_pipeline(
        player_name="A. Player",
        sport="MLB",
        stat_type="Hits",
        line=0.5,
        provider="Underdog",
        decision=type("Decision", (), {"recommendation": "OVER", "confidence": 80})(),
        analyst=FakeProvider(),
    )
    assert result.prediction == "UNDER"
    assert result.model == "test-model"
    assert result.model_version == "test-1"


def test_prior_line_movements_are_advisory_context_only():
    decision = type("Decision", (), {
        "recommendation": "OVER",
        "confidence": 72,
        "risk_level": "MEDIUM",
        "evidence": {},
    })()
    movement_context = {
        "line_variant": "goblin",
        "scope": "prior_events",
        "events": [
            {
                "event_id": "previous-game",
                "lines": [{
                    "opening_line": 1.5,
                    "previous_line": 1.5,
                    "current_line": 2.0,
                    "movement_count": 1,
                }],
            }
        ],
    }

    without_context = build_advisory_from_pipeline(
        player_name="A. Player",
        sport="MLB",
        stat_type="Hits",
        line=1.5,
        provider="PrizePicks",
        decision=decision,
    )
    with_context = build_advisory_from_pipeline(
        player_name="A. Player",
        sport="MLB",
        stat_type="Hits",
        line=1.5,
        provider="PrizePicks",
        decision=decision,
        line_movement_context=movement_context,
    )

    assert with_context.prediction == without_context.prediction == "OVER"
    assert with_context.confidence == without_context.confidence == 72
    assert with_context.line_movement_context == movement_context
    assert "prior_line_movements" in with_context.evidence_used


@pytest.mark.parametrize(
    ("prediction", "result", "recommendation", "expected"),
    [
        ("UNDER", "MISS", "OVER", "HIT"),
        ("UNDER", "HIT", "UNDER", "HIT"),
        ("OVER", "HIT", "UNDER", "MISS"),
        ("OVER", "MISS", "UNDER", "HIT"),
    ],
)
def test_analyst_grade_translates_from_stored_pick_direction(
    prediction, result, recommendation, expected
):
    assert analyst_grade(prediction, result, recommendation) == expected


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sport", ["NBA", "NHL", "MLB"])
async def test_prediction_persists_and_grades_for_supported_sports(
    db: Database, sport: str
):
    payload = build_advisory_from_pipeline(
        player_name="A. Player",
        sport=sport,
        stat_type="Points",
        line=10.5,
        provider="Underdog",
        event_id=f"{sport}-event",
        decision=type("Decision", (), {
            "recommendation": "UNDER",
            "confidence": 74,
            "evidence": {"stored": True},
        })(),
    ).to_dict()
    await db.log_prop_opportunity(
        external_id=f"{sport}-prop",
        player_name="A. Player",
        team="TEAM",
        sport=sport,
        stat_type="Points",
        line_value=10.5,
        recommendation="UNDER",
        decision_tier="A",
        confidence=74,
        game_time=datetime.utcnow() - timedelta(hours=5),
        game_id=f"{sport}-event",
        provider="Underdog",
        analyst_result=payload,
    )

    async with db.session() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == f"{sport}-prop"
                )
            )
        ).scalar_one()
        assert row.analyst_prediction == "UNDER"
        assert row.analyst_model_version == "1"
        row_id = row.id

    # The stored result reflects the original UNDER recommendation, so a MISS
    # means this UNDER analyst call lost.
    await db.grade_opportunity(
        row_id, "MISS", 12.0, grade_result_source="propline_history"
    )
    report = await db.get_analyst_performance(
        group_by="sport", sport=sport, direction="UNDER"
    )
    assert report["buckets"][sport]["hits"] == 0
    assert report["buckets"][sport]["misses"] == 1
    assert report["buckets"][sport]["hit_rate"] == 0.0