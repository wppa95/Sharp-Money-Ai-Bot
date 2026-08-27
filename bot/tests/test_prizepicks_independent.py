"""Contract tests for independent PrizePicks persistence and presentation."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from alerts_multiplatform import format_underdog_change_alert
from commands import PropPickAdapter
from database import Database, PropOpportunityLog


@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _opportunity(provider: str) -> dict:
    return {
        "external_id": "shared-event-prop",
        "player_name": "LeBron James",
        "team": "LAL",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 25.5,
        "recommendation": "OVER",
        "decision_tier": "A",
        "confidence": 78,
        "game_time": datetime.utcnow(),
        "provider": provider,
        "bet_quality_score": 78,
    }


class TestIndependentPrizePicks:
    async def test_same_external_id_is_stored_once_per_provider(self, db):
        await db.log_prop_opportunity(**_opportunity("Underdog"))
        await db.log_prop_opportunity(**_opportunity("PrizePicks"))

        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(PropOpportunityLog).order_by(PropOpportunityLog.id)
                    )
                ).scalars()
            )

        assert len(rows) == 2
        assert {row.provider for row in rows} == {"Underdog", "PrizePicks"}
        assert {row.external_id for row in rows} == {
            "shared-event-prop",
            "PrizePicks::shared-event-prop",
        }

    async def test_alert_dedup_restore_keeps_provider_keys_separate(self, db):
        await db.log_prop_opportunity(**_opportunity("Underdog"))
        await db.log_prop_opportunity(**_opportunity("PrizePicks"))
        await db.mark_opportunity_alert_sent(
            "shared-event-prop", "Points", provider="Underdog"
        )
        await db.mark_opportunity_alert_sent(
            "shared-event-prop", "Points", provider="PrizePicks"
        )

        restored = await db.get_recent_alerted_props_for_dedup(since_hours=1)

        assert ("LeBron James", "NBA", "Points") in restored
        assert ("PrizePicks::LeBron James", "NBA", "Points") in restored

    def test_prizepicks_alert_is_explicitly_labeled(self):
        message = format_underdog_change_alert(
            player_name="LeBron James",
            team="LAL",
            sport="NBA",
            stat_type="Points",
            old_line=24.5,
            new_line=25.5,
            source="PrizePicks",
        )

        assert "PRIZEPICKS PICK" in message
        assert "PrizePicks Line" in message
        assert "UNDERDOG PROP LIVE" not in message

    def test_provider_adapter_preserves_prizepicks_identity(self):
        prop = SimpleNamespace(
            provider="PrizePicks",
            player_name="LeBron James",
            stat_type="Points",
            sport="NBA",
            team="LAL",
            game_time=None,
            fetched_at=datetime.utcnow(),
            line_value=25.5,
            prev_line=24.5,
        )

        adapter = PropPickAdapter(prop)

        assert adapter.sportsbook == "PrizePicks"
        assert adapter.pp_line_value == 25.5