"""Contract tests for independent PrizePicks persistence and presentation."""

from __future__ import annotations

from datetime import datetime
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from alerts_multiplatform import format_underdog_change_alert
from commands import PropPickAdapter
from database import Database, PropOpportunityLog
import market_engine
from engine.player_results import compute_hit_rates
from providers.propline import PropLineFetchResult


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
    async def test_authoritative_discovery_controls_scope_without_underdog_inheritance(
        self, monkeypatch
    ):
        discovered = [
            SimpleNamespace(canonical_key="NHL"),
            SimpleNamespace(canonical_key="SOCCER"),
            SimpleNamespace(canonical_key="WNBA"),
            SimpleNamespace(canonical_key="NFL"),
        ]

        class FakeProvider:
            async def discover_sports(self):
                return discovered

        # Even an explicit compatibility fallback and Underdog scope must not
        # narrow an authoritative PrizePicks catalog.
        monkeypatch.setattr(
            market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA,MLB"
        )
        monkeypatch.setattr(market_engine.config, "ACTIVE_SPORTS_RAW", "NBA,MLB")

        sports = await market_engine._get_prizepicks_scan_sports(FakeProvider())

        assert sports == ("NHL", "SOCCER", "WNBA")

    async def test_cycles_round_robin_one_configured_sport_with_scoped_fetch(
        self, monkeypatch
    ):
        calls = []

        class FakeProvider:
            def is_available(self):
                return True

            async def fetch_props_for_sport(self, sport):
                calls.append(sport)
                return []

            async def fetch_props(self):
                raise AssertionError("scoped provider fetch should be preferred")

        class FakeDB:
            async def get_prop_line_history(self, *args, **kwargs):
                return []

        monkeypatch.setattr(market_engine, "_propline_provider", FakeProvider())
        monkeypatch.setattr(market_engine, "_prizepicks_sport_cursor", 0)
        monkeypatch.setattr(
            market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA,MLB"
        )
        await market_engine._run_prizepicks_independent_cycle(
            SimpleNamespace(bot_data={"db": FakeDB()}, bot=object())
        )
        await market_engine._run_prizepicks_independent_cycle(
            SimpleNamespace(bot_data={"db": FakeDB()}, bot=object())
        )

        assert calls == ["NBA", "MLB"]

    async def test_failed_propline_fetch_is_not_recorded_as_provider_success(
        self, monkeypatch, caplog
    ):
        class FakeProvider:
            def is_available(self):
                return True

            async def fetch_props_for_sport_result(self, sport):
                return PropLineFetchResult([], datetime.utcnow(), succeeded=False)

        class FakeHealth:
            def __init__(self):
                self.successes = []
                self.failures = []

            def record_success(self, provider):
                self.successes.append(provider)

            def record_failure(self, provider, error, failure_type):
                self.failures.append((provider, error, failure_type))

        health = FakeHealth()
        monkeypatch.setattr(market_engine, "_propline_provider", FakeProvider())
        monkeypatch.setattr(market_engine, "_prizepicks_sport_cursor", 0)
        monkeypatch.setattr(
            market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA,MLB"
        )
        monkeypatch.setattr(market_engine, "get_health_monitor", lambda: health)

        with caplog.at_level(logging.INFO):
            await market_engine._run_prizepicks_independent_cycle(
                SimpleNamespace(bot_data={"db": object()}, bot=object())
            )

        assert health.successes == []
        assert health.failures
        assert health.failures[0][0] == "PrizePicks"
        assert "provider=PrizePicks sport=NBA fetch_succeeded=false" in caplog.text

    async def test_cycle_accepts_persisted_history_with_removed_field(self, monkeypatch):
        prop = SimpleNamespace(
            provider="PrizePicks",
            player_name="Cade Cunningham",
            team="DET",
            sport="NBA",
            stat_type="Points",
            line_value=10.5,
            game_time=None,
            external_id="pp-cade-points",
            event_id="event-1",
        )
        now = datetime.utcnow()
        history_rows = [
            SimpleNamespace(
                line_value=9.5,
                prev_line=9.0,
                change_count=1,
                removed=False,
                fetched_at=now,
            )
            for _ in range(30)
        ]
        results = [
            SimpleNamespace(
                player_name=prop.player_name,
                stat_type=prop.stat_type,
                actual_value=12.0,
                game_date=(now.date()).isoformat(),
                opponent="TEST",
            )
            for _ in range(10)
        ]
        hit_rates = compute_hit_rates(results, prop.line_value)

        class FakeDB:
            def __init__(self):
                self.logged = []
                self.saved = []
                self.candidates = []

            async def get_prop_line_history(self, provider, player, sport, stat, limit=30):
                assert provider == "PrizePicks"
                return history_rows

            async def log_prop_opportunity(self, **kwargs):
                self.logged.append(kwargs)

            async def log_prop_candidate_batch(self, rows):
                self.candidates.extend(rows)
                return len(rows)

            async def upsert_prop_line_history_batch(self, provider, props):
                self.saved.append((provider, list(props)))

            async def mark_opportunity_alert_sent(self, *args, **kwargs):
                pass

            async def update_prop_lifecycle_state(self, *args, **kwargs):
                pass

        class FakeProvider:
            def is_available(self):
                return True

            async def fetch_props(self):
                return [prop]

        class FakeDelivery:
            def __init__(self, db, bot, chat_ids):
                pass

            async def deliver_underdog(self, **kwargs):
                return SimpleNamespace(sent=False)

        async def fake_enrichment(*args, **kwargs):
            return hit_rates

        async def claim_slot(*args, **kwargs):
            return True

        db = FakeDB()
        monkeypatch.setattr(market_engine, "_propline_provider", FakeProvider())
        # Providers without authoritative discovery must opt into an explicit
        # compatibility scope; the production default is provider-discovered.
        monkeypatch.setattr(
            market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA"
        )
        monkeypatch.setattr(market_engine, "_fetch_and_compute_hit_rates", fake_enrichment)
        monkeypatch.setattr(market_engine, "AlertDelivery", FakeDelivery)
        monkeypatch.setattr(market_engine, "_try_claim_delivery_slot", claim_slot)

        await market_engine._run_prizepicks_independent_cycle(
            SimpleNamespace(bot_data={"db": db}, bot=object())
        )

        assert db.saved and db.saved[0][0] == "PrizePicks"
        assert db.logged
        assert db.logged[0]["provider"] == "PrizePicks"
        assert db.candidates
        assert all(row["provider"] == "PrizePicks" for row in db.candidates)
        assert db.candidates[0]["gate_decision"] == "EVALUATED"

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
        assert "LINE</b> · PrizePicks" in message
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