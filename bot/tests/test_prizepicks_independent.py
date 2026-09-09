"""Contract tests for independent PrizePicks persistence and presentation."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from alerts_multiplatform import (
    _format_available_lines_block,
    format_underdog_change_alert,
)
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
    async def test_request_worker_skips_discovery_during_provider_cooldown(
        self, monkeypatch
    ):
        class CooldownProvider:
            backoff_remaining = 60.0
            client = None

            def is_available(self):
                return True

            async def discover_sports(self):
                raise AssertionError("cooldown must prevent sport discovery")

        monkeypatch.setattr(market_engine, "_propline_provider", CooldownProvider())

        await market_engine.propline_request_scan_job(
            SimpleNamespace(bot_data={"db": None})
        )

    async def test_request_worker_persists_non_empty_observations(self, monkeypatch):
        prop = SimpleNamespace(
            player_name="Fetched Player",
            team="TM",
            sport="NBA",
            stat_type="Points",
            line_value=20.5,
            game_time=None,
            external_id="fetched-prop",
            event_id="fetched-event",
            fetched_at=datetime.utcnow(),
        )

        class FakeProvider:
            client = None

            def is_available(self):
                return True

            async def fetch_props_for_sport_result(self, sport, bookmaker=None):
                assert sport == "NBA"
                assert bookmaker == "prizepicks"
                return PropLineFetchResult(
                    [prop], datetime.utcnow(), succeeded=True, complete=True
                )

        class FakeDB:
            def __init__(self):
                self.saved = []
                self.tracked = []

            async def upsert_prop_line_history_batch(self, provider, props, **kwargs):
                self.saved.append((provider, list(props), kwargs))

            async def track_prop_records(self, records):
                self.tracked.extend(records)

        db = FakeDB()
        monkeypatch.setattr(market_engine, "_propline_provider", FakeProvider())
        monkeypatch.setattr(market_engine, "_propline_request_cursor", 0)
        monkeypatch.setattr(
            market_engine,
            "_get_prizepicks_scan_sports",
            AsyncMock(return_value=("NBA",)),
        )

        await market_engine.propline_request_scan_job(
            SimpleNamespace(bot_data={"db": db})
        )

        assert len(db.saved) == 1
        assert db.saved[0][0] == "PrizePicks"
        assert db.saved[0][2] == {"append_snapshot": True}
        assert len(db.tracked) == 1
        assert db.tracked[0]["external_id"] == "fetched-prop"

    def test_available_lines_show_verified_prizepicks_or_explicit_unavailable(self):
        available = _format_available_lines_block(15.5, pp_line=14.5)
        unavailable = _format_available_lines_block(15.5)

        assert "🎯 Player Prop:  <code>14.5</code>" in available
        assert "🎯 Player Prop:  <code>15.5</code>" in available
        assert "🎯 Player Prop:  <i>Unavailable for this prop</i>" in unavailable
        assert "🎯 Player Prop:  <code>15.5</code>" in unavailable

    def test_provider_alerts_do_not_mix_prizepicks_and_underdog_lines(self):
        underdog_message = format_underdog_change_alert(
            "Jake Bennett",
            "DET",
            "MLB",
            "Walks Allowed",
            0.5,
            1.5,
            pp_line=1.5,
            source="Underdog",
        )
        prizepicks_message = format_underdog_change_alert(
            "Jake Bennett",
            "DET",
            "MLB",
            "Walks Allowed",
            0.5,
            1.5,
            pp_line=1.5,
            source="PrizePicks",
        )

        assert "📡 <b>Source:</b>  Player Props · Underdog" in underdog_message
        assert "📡 <b>Source:</b>  Player Props · PrizePicks" in prizepicks_message

    def test_confirmation_line_only_accepts_verified_prizepicks_source(self):
        assert market_engine._prizepicks_line_from_confirmation(
            {"source": "PrizePicks", "confirmed": True, "avg_line": 14.5}
        ) == 14.5
        assert market_engine._prizepicks_line_from_confirmation(
            {"source": "OddsAPI", "confirmed": True, "avg_line": 14.5}
        ) is None
        assert market_engine._prizepicks_line_from_confirmation(None) is None

    async def test_current_pool_matching_normalizes_stat_labels_and_keeps_line_difference(
        self,
    ):
        class CurrentPoolDB:
            async def get_all_active_prop_line_history_by_line(self, providers=None):
                return {
                    (
                        "PrizePicks",
                        "Kiki Rice",
                        "WNBA",
                        "points",
                        15.5,
                        "pp-event-1",
                    ): SimpleNamespace(
                        provider="PrizePicks",
                        player_name="Kiki Rice",
                        sport="WNBA",
                        stat_type="points",
                        line_value=15.5,
                        game_id="pp-event-1",
                        external_id="pp-prop-1",
                        fetched_at=datetime.utcnow(),
                        removed=False,
                    )
                }

        result = await market_engine._get_odds_api_confirmation(
            "WNBA",
            "Kiki Rice",
            "Points",
            "UNDER",
            16.5,
            db=CurrentPoolDB(),
        )

        assert result is not None
        assert result["source"] == "PrizePicks"
        assert result["avg_line"] == 15.5

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

        assert sports == ("NHL", "SOCCER", "WNBA", "NFL")

    async def test_catalog_rate_limit_falls_back_to_all_known_provider_sports(self):
        class RateLimitedClient:
            api_url = "https://api.prop-line.com"
            is_configured = True
            sports_discovery_failed = True

            async def discover_sports(self):
                return []

        from providers.propline import PropLinePrizePicksProvider

        provider = PropLinePrizePicksProvider(client=RateLimitedClient())
        sports = await market_engine._get_prizepicks_scan_sports(provider)

        assert "NBA" in sports
        assert "MLB" in sports
        assert "TENNIS" in sports
        assert "NFL" in sports
        assert "NCAAF" in sports
        assert "CFL" in sports

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

    async def test_result_fetch_adapter_also_stays_one_sport_per_cycle(
        self, monkeypatch
    ):
        calls = []
        feed_counts = []

        class FakeProvider:
            def is_available(self):
                return True

            async def fetch_props_for_sport_result(self, sport, bookmaker=None):
                calls.append((sport, bookmaker))
                return PropLineFetchResult([], datetime.utcnow(), succeeded=True)

        class FakeHealthTracker:
            def record_provider_fetch(self, provider):
                pass

            def record_provider_feed_counts(
                self, provider, sport, raw_records, normalized_props, *, succeeded
            ):
                feed_counts.append(
                    (provider, sport, raw_records, normalized_props, succeeded)
                )

        class FakeDB:
            async def get_prop_line_history(self, *args, **kwargs):
                return []

        monkeypatch.setattr(market_engine, "_propline_provider", FakeProvider())
        monkeypatch.setattr(market_engine, "_prizepicks_sport_cursor", 0)
        monkeypatch.setattr(
            market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA,MLB,WNBA"
        )
        monkeypatch.setattr(
            market_engine, "get_health_tracker", lambda: FakeHealthTracker()
        )

        await market_engine._run_prizepicks_independent_cycle(
            SimpleNamespace(bot_data={"db": FakeDB()}, bot=object())
        )

        assert calls == [("NBA", "prizepicks")]
        assert feed_counts == [("PrizePicks", "NBA", 0, 0, True)]

    async def test_current_normalized_props_reach_universal_tracking(
        self, monkeypatch, caplog
    ):
        prop = SimpleNamespace(
            provider="PrizePicks",
            player_name="Current Feed Player",
            team="TM",
            sport="NBA",
            stat_type="points",
            line_value=20.5,
            game_time=None,
            external_id="current-feed-prop",
            event_id="current-feed-event",
            fetched_at=datetime.utcnow(),
        )

        class FakeProvider:
            def is_available(self):
                return True

            async def fetch_props_for_sport_result(self, sport, bookmaker=None):
                assert sport == "NBA"
                assert bookmaker == "prizepicks"
                return PropLineFetchResult(
                    [prop], datetime.utcnow(), succeeded=True, complete=True
                )

        class FakeDB:
            def __init__(self):
                self.saved = []
                self.tracked = []

            async def get_prop_line_history(self, *args, **kwargs):
                return []

            async def upsert_prop_line_history_batch(self, provider, props, **kwargs):
                self.saved.append((provider, list(props), kwargs))
                return len(props)

            async def track_prop_records(self, records):
                self.tracked.extend(records)

        monkeypatch.setattr(market_engine, "_propline_provider", FakeProvider())
        monkeypatch.setattr(market_engine, "_prizepicks_sport_cursor", 0)
        monkeypatch.setattr(
            market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA"
        )
        monkeypatch.setattr(
            market_engine, "_fetch_and_compute_hit_rates", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(market_engine, "get_health_tracker", lambda: None)
        monkeypatch.setattr(market_engine, "get_health_monitor", lambda: None)

        db = FakeDB()
        with caplog.at_level(logging.INFO):
            await market_engine._run_prizepicks_independent_cycle(
                SimpleNamespace(bot_data={"db": db}, bot=object())
            )

        assert len(db.saved) == 2
        assert db.saved[0][0] == "PrizePicks"
        assert db.saved[0][2] == {"append_snapshot": True}
        assert db.saved[1][0] == "PrizePicks"
        assert db.saved[1][2] == {"refresh_metadata": True}
        assert len(db.tracked) == 1
        assert db.tracked[0]["provider"] == "PrizePicks"
        assert "observations=1 persisted=1" in caplog.text

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
        for provider in ("Underdog", "PrizePicks"):
            await db.log_telegram_delivery(
                provider=provider,
                delivery_path="standing",
                player_name="LeBron James",
                sport="NBA",
                stat_type="Points",
                external_id="shared-event-prop",
                event_id=f"game-{provider.lower()}",
                line_value=24.5,
                outcome="sent",
                recipients_sent=1,
            )

        restored = await db.get_recent_alerted_props_for_dedup(since_hours=1)

        assert ("LeBron James", "NBA", "Points") in restored
        assert ("PrizePicks::LeBron James", "NBA", "Points") in restored

    def test_player_prop_alert_uses_unified_label(self):
        message = format_underdog_change_alert(
            player_name="LeBron James",
            team="LAL",
            sport="NBA",
            stat_type="Points",
            old_line=24.5,
            new_line=25.5,
            source="PrizePicks",
        )

        assert "PLAYER PROP" in message
        assert "PLAYER PROP LINE" in message
        assert "📡 <b>Source:</b>  Player Props · PrizePicks" in message
        assert "Underdog" not in message

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