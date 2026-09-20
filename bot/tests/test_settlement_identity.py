"""Regression tests for provider/event-scoped player result grading."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from database import Database


@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _result(game_id: str, actual_value: float) -> SimpleNamespace:
    return SimpleNamespace(
        player_name="Same Player",
        sport="NBA",
        stat_type="points",
        game_date="2026-08-20",
        actual_value=actual_value,
        opponent="OPP",
        source="test_stats",
        game_id=game_id,
    )


def _opportunity(provider: str, game_id: str, external_id: str) -> dict:
    return {
        "external_id": external_id,
        "player_name": "Same Player",
        "team": "TEAM",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 20.5,
        "recommendation": "OVER",
        "decision_tier": "A",
        "confidence": 80,
        "game_time": datetime(2026, 8, 20),
        "game_id": game_id,
        "provider": provider,
    }


class TestSettlementIdentity:
    async def test_pending_opportunity_sport_cursors_interleave_large_backlogs(self, db):
        """A dominant sport must not consume the entire grading page."""
        for index in range(30):
            await db.log_prop_opportunity(
                **(
                    _opportunity(
                        "Underdog",
                        f"mlb-event-{index}",
                        f"mlb-prop-{index}",
                    )
                    | {"sport": "MLB"}
                )
            )
        for index in range(3):
            await db.log_prop_opportunity(
                **(
                    _opportunity(
                        "Underdog",
                        f"nba-event-{index}",
                        f"nba-prop-{index}",
                    )
                    | {"sport": "NBA"}
                )
            )
            await db.log_prop_opportunity(
                **(
                    _opportunity(
                        "Underdog",
                        f"nfl-event-{index}",
                        f"nfl-prop-{index}",
                    )
                    | {"sport": "NFL"}
                )
            )

        rows = await db.get_pending_opportunities(
            cutoff_hours=0.0,
            limit=9,
            sport_cursors={"MLB": 0, "NBA": 0, "NFL": 0},
        )

        assert len(rows) == 9
        assert {row.sport for row in rows} == {"MLB", "NBA", "NFL"}
        assert sum(row.sport == "MLB" for row in rows) == 3
        assert sum(row.sport == "NBA" for row in rows) == 3
        assert sum(row.sport == "NFL" for row in rows) == 3

    async def test_same_player_date_can_store_two_event_results(self, db):
        await db.upsert_player_result(_result("event-a", 25.0))
        await db.upsert_player_result(_result("event-b", 10.0))

        rows = await db.get_player_results("Same Player", "NBA", "Points", limit=10)

        assert len(rows) == 2
        assert {row.game_id for row in rows} == {"event-a", "event-b"}

    async def test_lookup_does_not_cross_unrelated_events(self, db):
        await db.upsert_player_result(_result("event-a", 25.0))
        await db.upsert_player_result(_result("event-b", 10.0))

        event_a = await db.get_game_result_for_grading(
            "Same Player", "NBA", "Points", "2026-08-20", game_id="event-a"
        )
        missing = await db.get_game_result_for_grading(
            "Same Player", "NBA", "Points", "2026-08-20", game_id="event-c"
        )

        assert event_a is not None
        assert event_a.actual_value == 25.0
        assert missing is None

    async def test_same_player_stat_on_two_providers_uses_each_event(self, db):
        await db.log_prop_opportunity(
            **_opportunity("Underdog", "ud-event", "shared-prop")
        )
        await db.log_prop_opportunity(
            **_opportunity("PrizePicks", "pp-event", "shared-prop")
        )
        await db.upsert_player_result(_result("ud-event", 25.0))
        await db.upsert_player_result(_result("pp-event", 10.0))

        async with db.session() as session:
            from sqlalchemy import select
            from database import PropOpportunityLog

            rows = list(
                (
                    await session.execute(
                        select(PropOpportunityLog).order_by(PropOpportunityLog.id)
                    )
                ).scalars()
            )

        assert [(row.provider, row.game_id) for row in rows] == [
            ("Underdog", "ud-event"),
            ("PrizePicks", "pp-event"),
        ]
        assert (
            await db.get_game_result_for_grading(
                rows[0].player_name,
                rows[0].sport,
                rows[0].stat_type,
                rows[0].game_time.strftime("%Y-%m-%d"),
                game_id=rows[0].game_id,
                provider=rows[0].provider,
            )
        ).actual_value == 25.0
        assert (
            await db.get_game_result_for_grading(
                rows[1].player_name,
                rows[1].sport,
                rows[1].stat_type,
                rows[1].game_time.strftime("%Y-%m-%d"),
                game_id=rows[1].game_id,
                provider=rows[1].provider,
            )
        ).actual_value == 10.0