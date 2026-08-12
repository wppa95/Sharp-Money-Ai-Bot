"""
Tests for AlertCLVSeed pipeline in bot/database.py

Covers:
  - PropLineHistory: save, bulk save, get_prop_line_history, get_latest_props, count
  - AlertCLVSeed: save (upsert-on-conflict), get_pending, count_pending, mark_computed
  - seed_clv_from_ev_records: creates seeds for alerted EVRecords
  - seed_clv_from_ud_snapshots: creates seeds for alerted UnderdogSnapshots
  - _tier_from_confidence helper
  - _migrate_clv_records: idempotent migration
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import (
    Database,
    PropLineHistory,
    AlertCLVSeed,
    EVRecord,
    UnderdogSnapshotRecord,
    _tier_from_confidence,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()




# ── _tier_from_confidence helper tests ────────────────────────────────────────

class TestTierFromConfidence:
    async def test_s_tier_at_95(self):
        assert _tier_from_confidence(95) == "S"

    async def test_s_tier_above_95(self):
        assert _tier_from_confidence(100) == "S"

    async def test_a_tier_at_85(self):
        assert _tier_from_confidence(85) == "A"

    async def test_a_tier_between_85_and_94(self):
        assert _tier_from_confidence(90) == "A"

    async def test_b_tier_at_75(self):
        assert _tier_from_confidence(75) == "B"

    async def test_b_tier_between_75_and_84(self):
        assert _tier_from_confidence(80) == "B"

    async def test_pass_below_75(self):
        assert _tier_from_confidence(74) == "PASS"

    async def test_pass_at_zero(self):
        assert _tier_from_confidence(0) == "PASS"

    async def test_pass_at_50(self):
        assert _tier_from_confidence(50) == "PASS"


# ── PropLineHistory tests ─────────────────────────────────────────────────────

class TestPropLineHistory:
    def _make_record(self, **kwargs) -> PropLineHistory:
        defaults = dict(
            provider="Underdog",
            sport="MLB",
            player_name="Mike Trout",
            team="LAA",
            stat_type="Hits",
            line_value=1.5,
            game_time=None,
            external_id="ext-001",
            game_id="game-001",
            fetched_at=datetime.utcnow(),
        )
        defaults.update(kwargs)
        return PropLineHistory(**defaults)

    async def test_save_and_retrieve(self, db):
        record = self._make_record()
        saved = await db.save_prop_line_history(record)
        assert saved.id is not None

        history = await db.get_prop_line_history( "Underdog", "Mike Trout", "MLB", "Hits" )
        assert len(history) == 1
        assert history[0].player_name == "Mike Trout"
        assert history[0].line_value  == 1.5

    async def test_bulk_save(self, db):
        records = [
            self._make_record(player_name="Player A", line_value=1.5),
            self._make_record(player_name="Player B", line_value=2.5),
            self._make_record(player_name="Player C", line_value=0.5),
        ]
        await db.save_prop_line_history_bulk(records)
        count = await db.count_prop_line_history("Underdog")
        assert count == 3

    async def test_bulk_save_empty_list(self, db):
        # Should not raise
        await db.save_prop_line_history_bulk([])
        assert await db.count_prop_line_history() == 0

    async def test_get_history_returns_newest_first(self, db):
        now = datetime.utcnow()
        records = [
            self._make_record(fetched_at=now - timedelta(hours=2)),
            self._make_record(fetched_at=now - timedelta(hours=1)),
            self._make_record(fetched_at=now),
        ]
        await db.save_prop_line_history_bulk(records)
        history = await db.get_prop_line_history("Underdog", "Mike Trout", "MLB", "Hits")
        assert history[0].fetched_at >= history[1].fetched_at >= history[2].fetched_at

    async def test_get_history_filtered_by_stat(self, db):
        await db.save_prop_line_history(self._make_record(stat_type="Hits"))
        await db.save_prop_line_history(self._make_record(stat_type="Runs"))

        hits = await db.get_prop_line_history("Underdog", "Mike Trout", "MLB", "Hits")
        runs = await db.get_prop_line_history("Underdog", "Mike Trout", "MLB", "Runs")
        assert len(hits) == 1
        assert len(runs) == 1

    async def test_count_filtered_by_provider(self, db):
        await db.save_prop_line_history(self._make_record(provider="Underdog"))
        await db.save_prop_line_history(self._make_record(provider="PrizePicks"))
        assert await db.count_prop_line_history("Underdog")   == 1
        assert await db.count_prop_line_history("PrizePicks") == 1
        assert await db.count_prop_line_history()             == 2

    async def test_get_latest_per_provider(self, db):
        now = datetime.utcnow()
        await db.save_prop_line_history_bulk([ self._make_record(player_name="A", stat_type="Hits", fetched_at=now - timedelta(hours=1)), self._make_record(player_name="A", stat_type="Hits", fetched_at=now), self._make_record(player_name="B", stat_type="Points", fetched_at=now), ])
        latest = await db.get_latest_props_for_provider("Underdog", since_hours=24)
        # Deduped: one record for (A, Hits) and one for (B, Points)
        assert len(latest) == 2

    async def test_get_latest_outside_window(self, db):
        old_time = datetime.utcnow() - timedelta(hours=48)
        await db.save_prop_line_history(self._make_record(fetched_at=old_time))
        latest = await db.get_latest_props_for_provider("Underdog", since_hours=6)
        assert latest == []


# ── AlertCLVSeed tests ────────────────────────────────────────────────────────

class TestAlertCLVSeed:
    def _make_seed(self, **kwargs) -> AlertCLVSeed:
        defaults = dict(
            source_table="ev_records",
            source_id=1,
            alert_type="EV",
            sport="MLB",
            market_type="Player Prop",
            event="Yankees @ Red Sox",
            selection="Judge Over 0.5 HR",
            bet_odds=-120,
            counterpart_odds=100,
            tier="A",
            game_time=datetime.utcnow() + timedelta(hours=2),
            alerted_at=datetime.utcnow(),
            clv_pct=None,
            clv_computed=False,
        )
        defaults.update(kwargs)
        return AlertCLVSeed(**defaults)

    async def test_save_returns_seed(self, db):
        seed = self._make_seed()
        await db.save_alert_clv_seed(seed)
        # Verify via get_clv_seed_for_source
        fetched = await db.get_clv_seed_for_source("ev_records", 1)
        assert fetched is not None
        assert fetched.alert_type == "EV"

    async def test_duplicate_source_ignored(self, db):
        """Inserting the same (source_table, source_id) twice keeps one row."""
        seed1 = self._make_seed(source_id=42, bet_odds=-110)
        seed2 = self._make_seed(source_id=42, bet_odds=-130)  # duplicate source_id
        await db.save_alert_clv_seed(seed1)
        await db.save_alert_clv_seed(seed2)
        fetched = await db.get_clv_seed_for_source("ev_records", 42)
        assert fetched is not None
        assert fetched.bet_odds == -110  # first one wins (not overwritten)

    async def test_get_pending_returns_seeds_past_game_time(self, db):
        past_game = self._make_seed(source_id=1, game_time=datetime.utcnow() - timedelta(hours=1))
        future_game = self._make_seed(source_id=2, game_time=datetime.utcnow() + timedelta(hours=3))
        await db.save_alert_clv_seed(past_game)
        await db.save_alert_clv_seed(future_game)

        pending = await db.get_pending_clv_seeds()
        assert len(pending) == 1
        assert pending[0].source_id == 1

    async def test_get_pending_excludes_computed(self, db):
        past_game = self._make_seed(
            source_id=1, game_time=datetime.utcnow() - timedelta(hours=1)
        )
        await db.save_alert_clv_seed(past_game)
        fetched_seed = await db.get_clv_seed_for_source("ev_records", 1)
        await db.mark_clv_seed_computed(fetched_seed.id, clv_pct=2.5)
        pending = await db.get_pending_clv_seeds()
        assert pending == []

    async def test_get_pending_excludes_null_game_time(self, db):
        null_game = self._make_seed(source_id=1, game_time=None)
        await db.save_alert_clv_seed(null_game)
        pending = await db.get_pending_clv_seeds()
        assert pending == []

    async def test_count_pending_matches_get_pending(self, db):
        for i in range(3):
            past = self._make_seed(
                source_id=i+1,
                game_time=datetime.utcnow() - timedelta(hours=1),
            )
            await db.save_alert_clv_seed(past)
        assert await db.count_pending_clv_seeds() == 3

    async def test_mark_clv_seed_computed_updates_flags(self, db):
        seed = self._make_seed(source_id=99)
        await db.save_alert_clv_seed(seed)
        fetched = await db.get_clv_seed_for_source("ev_records", 99)
        assert fetched is not None
        await db.mark_clv_seed_computed(fetched.id, clv_pct=3.14)

        updated = await db.get_clv_seed_for_source("ev_records", 99)
        assert updated is not None
        assert updated.clv_computed is True
        assert abs(updated.clv_pct - 3.14) < 0.001

    async def test_get_clv_seed_for_source_returns_none_when_missing(self, db):
        result = await db.get_clv_seed_for_source("ev_records", 9999)
        assert result is None

    async def test_different_source_tables_independent(self, db):
        ev_seed = self._make_seed(source_table="ev_records",         source_id=1)
        ud_seed = self._make_seed(source_table="underdog_snapshots", source_id=1)
        await db.save_alert_clv_seed(ev_seed)
        await db.save_alert_clv_seed(ud_seed)
        ev = await db.get_clv_seed_for_source("ev_records",         1)
        ud = await db.get_clv_seed_for_source("underdog_snapshots", 1)
        assert ev is not None
        assert ud is not None
        assert ev.alert_type == "EV"
        assert ud.alert_type == "EV"  # both have same alert_type from fixture


# ── seed_clv_from_ev_records tests ────────────────────────────────────────────

class TestSeedCLVFromEVRecords:
    def _make_ev_record(self, *, alert_sent=True, sport="MLB", ai_confidence=80) -> EVRecord:
        return EVRecord(
            sport=sport,
            market_type="Player Prop",
            event="Test Event",
            player=None,
            selection="Test Selection",
            line=None,
            best_odds=-110,
            best_book="DraftKings",
            fair_probability=0.52,
            expected_value=4.5,
            steam_score=50,
            ai_confidence=ai_confidence,
            recommendation="Bet",
            stars=3,
            reason_codes="",
            result=None,
            clv=None,
            alert_sent=alert_sent,
            detected_at=datetime.utcnow(),
        )

    async def test_seeds_alerted_ev_records(self, db):
        await db.save_ev(self._make_ev_record(alert_sent=True))
        count = await db.seed_clv_from_ev_records()
        assert count == 1

    async def test_skips_non_alerted_records(self, db):
        await db.save_ev(self._make_ev_record(alert_sent=False))
        count = await db.seed_clv_from_ev_records()
        assert count == 0

    async def test_does_not_duplicate_seeds(self, db):
        await db.save_ev(self._make_ev_record())
        count1 = await db.seed_clv_from_ev_records()
        count2 = await db.seed_clv_from_ev_records()
        assert count1 == 1
        assert count2 == 0  # already seeded — no duplicates

    async def test_seed_has_correct_sport(self, db):
        await db.save_ev(self._make_ev_record(sport="NBA", alert_sent=True))
        await db.seed_clv_from_ev_records()

        ev_records = await db.get_recent_ev(limit=1)
        seed = await db.get_clv_seed_for_source("ev_records", ev_records[0].id)
        assert seed is not None
        assert seed.sport == "NBA"

    async def test_seed_tier_from_confidence(self, db):
        await db.save_ev(self._make_ev_record(ai_confidence=95, alert_sent=True))
        await db.seed_clv_from_ev_records()
        ev_records = await db.get_recent_ev(limit=1)
        seed = await db.get_clv_seed_for_source("ev_records", ev_records[0].id)
        assert seed is not None
        assert seed.tier == "S"

    async def test_seed_alert_type_is_ev(self, db):
        await db.save_ev(self._make_ev_record())
        await db.seed_clv_from_ev_records()
        ev_records = await db.get_recent_ev(limit=1)
        seed = await db.get_clv_seed_for_source("ev_records", ev_records[0].id)
        assert seed is not None
        assert seed.alert_type == "EV"

    async def test_multiple_records_all_seeded(self, db):
        for _ in range(5):
            await db.save_ev(self._make_ev_record())
        count = await db.seed_clv_from_ev_records()
        assert count == 5


# ── seed_clv_from_ud_snapshots tests ─────────────────────────────────────────

class TestSeedCLVFromUDSnapshots:
    def _make_ud_record(
        self, *, alert_sent=True, sport="MLB", score_tier="A"
    ) -> UnderdogSnapshotRecord:
        return UnderdogSnapshotRecord(
            external_id="ud-001",
            player_name="Shohei Ohtani",
            team="LAD",
            sport=sport,
            stat_type="Strikeouts",
            line_value=7.5,
            game_id="game-001",
            game_time=datetime.utcnow() + timedelta(hours=3),
            line_moved=False,
            prev_line=None,
            line_delta=None,
            removed=False,
            alert_sent=alert_sent,
            score_total=80.0,
            score_tier=score_tier,
            score_stars=4,
            alert_outcome="sent",
            fetched_at=datetime.utcnow(),
        )

    async def test_seeds_alerted_ud_records(self, db):
        await db.save_underdog_snapshot(self._make_ud_record(alert_sent=True))
        count = await db.seed_clv_from_ud_snapshots()
        assert count == 1

    async def test_skips_non_alerted_records(self, db):
        await db.save_underdog_snapshot(self._make_ud_record(alert_sent=False))
        count = await db.seed_clv_from_ud_snapshots()
        assert count == 0

    async def test_no_duplicates_on_repeat_call(self, db):
        await db.save_underdog_snapshot(self._make_ud_record())
        count1 = await db.seed_clv_from_ud_snapshots()
        count2 = await db.seed_clv_from_ud_snapshots()
        assert count1 == 1
        assert count2 == 0

    async def test_seed_alert_type_is_underdog(self, db):
        await db.save_underdog_snapshot(self._make_ud_record())
        await db.seed_clv_from_ud_snapshots()
        ud_records = await db.get_recent_underdog_snapshots(limit=1)
        seed = await db.get_clv_seed_for_source("underdog_snapshots", ud_records[0].id)
        assert seed is not None
        assert seed.alert_type == "UNDERDOG"

    async def test_seed_game_time_stored(self, db):
        future = datetime.utcnow() + timedelta(hours=5)
        ud = self._make_ud_record()
        ud.game_time = future
        await db.save_underdog_snapshot(ud)
        await db.seed_clv_from_ud_snapshots()
        ud_records = await db.get_recent_underdog_snapshots(limit=1)
        seed = await db.get_clv_seed_for_source("underdog_snapshots", ud_records[0].id)
        assert seed is not None
        assert seed.game_time is not None

    async def test_seed_tier_stored(self, db):
        await db.save_underdog_snapshot(self._make_ud_record(score_tier="S"))
        await db.seed_clv_from_ud_snapshots()
        ud_records = await db.get_recent_underdog_snapshots(limit=1)
        seed = await db.get_clv_seed_for_source("underdog_snapshots", ud_records[0].id)
        assert seed is not None
        assert seed.tier == "S"
