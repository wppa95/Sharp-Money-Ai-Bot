from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_tracked_prop_grade_summary_buckets_pending_by_scheduled_game_time():
    from database import Database, PropTrackingLog

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    now = datetime.utcnow()
    game_times = [
        now + timedelta(days=1),
        now - timedelta(hours=2),
        now - timedelta(days=3),
        now - timedelta(days=10),
        None,
    ]
    try:
        async with db.session() as session:
            session.add_all([
                PropTrackingLog(
                    provider="Underdog",
                    external_id=f"age-bucket-{index}",
                    player_name=f"Player {index}",
                    sport="NBA",
                    stat_type="points",
                    line_value=10.5,
                    game_time=game_time,
                    game_id=f"event-{index}",
                )
                for index, game_time in enumerate(game_times)
            ])
            await session.commit()

        summary = await db.get_tracked_prop_grade_summary()

        assert summary["pending"] == 5
        assert summary["pending_by_event_age"] == {
            "upcoming": 1,
            "past_24h": 1,
            "past_1_7d": 1,
            "past_over_7d": 1,
            "unknown": 1,
        }
    finally:
        await db.close()