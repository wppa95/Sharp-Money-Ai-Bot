"""
Tests for the CLV harvest job logic.

Rather than running the full PTB Application, these tests exercise the
harvest logic in isolation using mock database objects.

Key invariants:
  - Seeds without bet_odds are expired immediately (pick'em / no market price)
  - Seeds without game_time are expired immediately (no timing info)
  - UNDERDOG seeds WITH bet_odds (from OddsAPI confirmation) proceed to
    closing-line lookup — NOT immediately expired.
  - Seeds with bet_odds + closing odds → compute CLV and mark computed
  - Seeds with bet_odds but no closing odds → expired after grace period
  - harvest job is idempotent (no side effects on already-computed seeds)
"""

from __future__ import annotations

import asyncio
import sys
import os
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import AlertCLVSeed, CLVRecord, Database

# ── Shared event loop ─────────────────────────────────────────────────────────





# ── In-memory DB fixture ──────────────────────────────────────────────────────

@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


# ── Helper: build a seed record ───────────────────────────────────────────────

def _make_seed(
    source_id:    int   = 1,
    alert_type:   str   = "EV",
    bet_odds:     Optional[int] = -110,
    game_time:    Optional[datetime] = None,
    tier:         str   = "A",
    sport:        str   = "NFL",
    market_type:  str   = "h2h",
    event:        str   = "Chiefs vs Ravens",
    selection:    str   = "Chiefs ML",
) -> AlertCLVSeed:
    return AlertCLVSeed(
        source_table     = "ev_records",
        source_id        = source_id,
        alert_type       = alert_type,
        sport            = sport,
        market_type      = market_type,
        event            = event,
        selection        = selection,
        bet_odds         = bet_odds,
        counterpart_odds = None,
        tier             = tier,
        game_time        = game_time,
        alerted_at       = datetime.utcnow(),   # NOT NULL in schema
        clv_computed     = False,
    )


# ── harvest logic (extracted from main._clv_harvest_job) ─────────────────────
# We test the logic, not the PTB context plumbing.

async def _run_harvest_logic(db: Database, now: datetime, grace: timedelta):
    """
    Minimal re-implementation of harvest logic from _clv_harvest_job for testing.
    Returns (harvested, expired) counts.
    """
    from engine.clv import compute_clv

    seeds = await db.get_pending_clv_seeds(limit=50)
    harvested = 0
    expired   = 0

    for seed in seeds:
        game_time = seed.game_time

        if game_time is None:
            await db.mark_clv_seed_expired(seed.id)
            expired += 1
            continue

        # Seeds without bet_odds cannot produce CLV%.
        # UNDERDOG seeds seeded via seed_clv_from_ud_confirmation() DO have
        # bet_odds (OddsAPI avg_odds) and must proceed to closing-line lookup.
        if not seed.bet_odds:
            await db.mark_clv_seed_expired(seed.id)
            expired += 1
            continue

        closing_record = await db.get_last_odds_for_event(
            seed.event or "", seed.selection or ""
        )

        if closing_record is not None and closing_record.american_odds:
            clv_result = compute_clv(
                bet_odds     = seed.bet_odds,
                closing_odds = closing_record.american_odds,
                selection    = seed.selection or "",
            )
            rec = CLVRecord(
                selection    = seed.selection or "",
                event        = seed.event     or "",
                sport        = seed.sport     or "",
                bet_odds     = seed.bet_odds,
                closing_odds = closing_record.american_odds,
                clv_pct      = clv_result.clv_pct,
                clv_proxy    = clv_result.clv_proxy,
                fair_prob_bet   = clv_result.fair_prob_bet,
                fair_prob_close = clv_result.fair_prob_close,
                notes        = "",
            )
            try:
                rec.alert_type  = seed.alert_type  or ""
                rec.market_type = seed.market_type or ""
                rec.tier        = seed.tier        or ""
            except AttributeError:
                pass
            await db.save_clv_record(rec)
            await db.mark_clv_seed_computed(seed.id, clv_result.clv_pct)
            harvested += 1
        elif now - game_time > grace:
            await db.mark_clv_seed_expired(seed.id)
            expired += 1

    return harvested, expired


class TestHarvestLogic:
    # Use real utcnow() so game_times computed as "X hours ago" are actually in the past
    NOW   = datetime.utcnow()
    GRACE = timedelta(hours=4)

    def _past_game(self, hours_ago: float) -> datetime:
        return datetime.utcnow() - timedelta(hours=hours_ago)

    # ── Seeds without game_time → immediately expired ─────────────────────────

    async def test_no_game_time_stale_expires(self, db):
        """Seeds with game_time=None appear in pending only after stale_hours (24h)."""
        seed = _make_seed(
            source_id  = 1,
            game_time  = None,
            # alerted_at must be > 24h ago to appear in pending
        )
        # Manually set alerted_at to 25h ago
        seed.alerted_at = self.NOW - timedelta(hours=25)
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert h == 0
        assert e == 1

    async def test_no_game_time_fresh_not_expired(self, db):
        """Seeds with game_time=None that are fresh (< 24h) should stay pending."""
        seed = _make_seed(source_id=1, game_time=None)
        # alerted_at = utcnow() (fresh) — default in _make_seed
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert h == 0
        assert e == 0  # not stale yet — stays pending

    # ── Seeds without bet_odds → expired immediately ──────────────────────────

    async def test_underdog_seed_no_bet_odds_expired(self, db):
        """Underdog pick'em seeds with no bet_odds are expired — CLV% not computable."""
        seed = _make_seed(
            source_id  = 2,
            alert_type = "UNDERDOG",
            bet_odds   = None,
            game_time  = self._past_game(2),
        )
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert e == 1
        assert h == 0

    async def test_underdog_seed_with_bet_odds_not_immediately_expired(self, db):
        """
        UNDERDOG seeds that have bet_odds (from OddsAPI confirmation via
        seed_clv_from_ud_confirmation) must NOT be immediately expired.
        They proceed to closing-line lookup, then sit in grace period.
        """
        seed = _make_seed(
            source_id  = 13,
            alert_type = "UNDERDOG",
            bet_odds   = -115,          # OddsAPI avg_odds populated at alert time
            game_time  = self._past_game(1),   # 1h ago, still within 4h grace
        )
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        # Within grace, no closing odds → left pending (not expired, not harvested)
        assert h == 0
        assert e == 0

    async def test_underdog_seed_with_bet_odds_expires_after_grace(self, db):
        """
        UNDERDOG seed with bet_odds that passes grace period without closing
        odds must be expired normally (same as EV seeds).
        """
        seed = _make_seed(
            source_id  = 14,
            alert_type = "UNDERDOG",
            bet_odds   = -115,
            game_time  = self._past_game(5),   # 5h ago > 4h grace
        )
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert e == 1
        assert h == 0

    async def test_ev_seed_no_bet_odds_expired(self, db):
        seed = _make_seed(
            source_id = 3,
            bet_odds  = None,
            game_time = self._past_game(2),
        )
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert e == 1

    # ── Seeds within grace period and no closing odds → not touched ───────────

    async def test_within_grace_no_odds_not_expired(self, db):
        seed = _make_seed(
            source_id = 4,
            bet_odds  = -110,
            game_time = self._past_game(1),   # 1h ago, grace is 4h
        )
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert h == 0
        assert e == 0  # Still within grace — left pending

    # ── Grace period elapsed and no closing odds → expired ────────────────────

    async def test_beyond_grace_no_odds_expires(self, db):
        seed = _make_seed(
            source_id = 5,
            bet_odds  = -110,
            game_time = self._past_game(5),   # 5h ago > 4h grace
        )
        await db.save_alert_clv_seed(seed)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert e == 1

    # ── get_pending_clv_seeds ignores already-computed seeds ──────────────────

    async def test_already_computed_seeds_not_re_harvested(self, db):
        """Seeds with clv_computed=True must not appear in pending."""
        seed = _make_seed(source_id=6, game_time=self._past_game(5))
        await db.save_alert_clv_seed(seed)
        fetched = await db.get_clv_seed_for_source("ev_records", 6)
        await db.mark_clv_seed_computed(fetched.id, 2.5)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert h == 0
        assert e == 0

    async def test_expired_seeds_not_re_harvested(self, db):
        seed = _make_seed(source_id=7, game_time=self._past_game(5))
        await db.save_alert_clv_seed(seed)
        fetched = await db.get_clv_seed_for_source("ev_records", 7)
        await db.mark_clv_seed_expired(fetched.id)

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert h == 0
        assert e == 0

    # ── Multiple seeds mixed ──────────────────────────────────────────────────

    async def test_mixed_seeds_correct_counts(self, db):
        """2 expired (stale/no bet_odds), 1 within grace (untouched)."""
        # Stale (game_time=None, alerted_at > 24h ago) → expire
        stale_seed = _make_seed(source_id=10, game_time=None)
        stale_seed.alerted_at = self.NOW - timedelta(hours=25)
        await db.save_alert_clv_seed(stale_seed)
        # UNDERDOG (no bet_odds) → expire
        await db.save_alert_clv_seed(_make_seed(
            source_id=11, alert_type="UNDERDOG", bet_odds=None,
            game_time=self._past_game(2),
        ))
        # Within grace → leave pending
        await db.save_alert_clv_seed(_make_seed(
            source_id=12, bet_odds=-110, game_time=self._past_game(1),
        ))

        h, e = await _run_harvest_logic(db, self.NOW, self.GRACE)
        assert e == 2
        assert h == 0

    # ── CLVRecord written correctly ───────────────────────────────────────────

    async def test_clv_record_count_before_harvest(self, db):
        initial = await db.count_clv_records()
        assert initial == 0

    # ── Pending count methods ─────────────────────────────────────────────────

    async def test_count_pending_after_expire(self, db):
        # Use a past game_time so seed appears in pending
        seed = _make_seed(source_id=20, game_time=self._past_game(5))
        await db.save_alert_clv_seed(seed)
        fetched = await db.get_clv_seed_for_source("ev_records", 20)
        assert fetched is not None

        before = await db.count_pending_clv_seeds()
        await db.mark_clv_seed_expired(fetched.id)
        after  = await db.count_pending_clv_seeds()

        assert before > after

    async def test_get_pending_filters_by_game_time(self, db):
        """Seeds whose game_time is in the future must NOT appear in pending."""
        future_seed = _make_seed(
            source_id = 99,
            bet_odds  = -110,
            game_time = self.NOW + timedelta(hours=2),
        )
        await db.save_alert_clv_seed(future_seed)

        pending = await db.get_pending_clv_seeds(limit=50)
        assert not any(s.source_id == 99 for s in pending)


class TestHarvestComputePath:
    """
    End-to-end tests for the CLV compute path:
    seed + closing OddsRecord → CLVRecord written, no AttributeError.

    These tests would have crashed before the clv_result.clv_lead →
    clv_result.clv_proxy fix because CLVResult has no `clv_lead` attribute.
    """

    def _past_game(self, hours_ago: float) -> datetime:
        return datetime.utcnow() - timedelta(hours=hours_ago)

    async def test_compute_path_writes_clv_record(self, db):
        """
        When a seed has bet_odds and a matching OddsRecord exists (closing odds),
        the harvest logic must compute CLV% and write a CLVRecord — no crash.
        """
        from database import OddsRecord

        # Create a seed past game_time
        seed = _make_seed(
            source_id  = 500,
            alert_type = "EV",
            bet_odds   = -110,
            game_time  = self._past_game(2),
            event      = "Chiefs vs Ravens",
            selection  = "Chiefs ML",
        )
        await db.save_alert_clv_seed(seed)

        # Create a matching OddsRecord (simulates closing-line data)
        closing = OddsRecord(
            event         = "Chiefs vs Ravens",
            selection     = "Chiefs ML",
            sport         = "NFL",
            market_type   = "h2h",
            sportsbook    = "fanduel",
            american_odds = -130,   # market moved from -110 → -130 (positive CLV)
            recorded_at   = datetime.utcnow(),
        )
        await db.save_odds(closing)

        now   = datetime.utcnow()
        grace = timedelta(hours=4)
        h, e  = await _run_harvest_logic(db, now, grace)

        assert h == 1, "CLVRecord should have been written"
        assert e == 0
        assert await db.count_clv_records() == 1

    async def test_compute_path_clv_pct_sign(self, db):
        """
        Bet at -110, closed at -130 → market tightened → positive CLV%.
        Verifies the math survives the full pipeline end-to-end.
        """
        from database import OddsRecord
        from engine.clv import compute_clv

        # Independently compute expected CLV%
        expected = compute_clv(bet_odds=-110, closing_odds=-130)
        assert expected.clv_pct > 0, "pre-condition: -110 vs -130 close should be +CLV"

        seed = _make_seed(
            source_id  = 501,
            alert_type = "EV",
            bet_odds   = -110,
            game_time  = self._past_game(2),
            event      = "Lakers vs Celtics",
            selection  = "Lakers ML",
        )
        await db.save_alert_clv_seed(seed)

        closing = OddsRecord(
            event         = "Lakers vs Celtics",
            selection     = "Lakers ML",
            sport         = "NBA",
            market_type   = "h2h",
            sportsbook    = "draftkings",
            american_odds = -130,
            recorded_at   = datetime.utcnow(),
        )
        await db.save_odds(closing)

        h, e = await _run_harvest_logic(db, datetime.utcnow(), timedelta(hours=4))
        assert h == 1

        # The seed should now be marked computed
        fetched = await db.get_clv_seed_for_source("ev_records", 501)
        assert fetched is not None
        assert fetched.clv_computed is True
        assert fetched.clv_pct is not None
        assert fetched.clv_pct > 0

    async def test_compute_path_underdog_seed_with_bet_odds(self, db):
        """
        UNDERDOG seed created via seed_clv_from_ud_confirmation should also
        be harvested correctly (no crash, CLVRecord written) when closing odds arrive.
        """
        from database import OddsRecord

        await db.seed_clv_from_ud_confirmation(
            source_id   = 502,
            sport       = "NBA",
            stat_type   = "points",
            player_name = "LeBron James",
            line        = 25.5,
            game_time   = self._past_game(2),
            tier        = "S",
            avg_odds    = -115,
        )

        # Simulate a closing OddsRecord for this player prop
        closing = OddsRecord(
            event         = "LeBron James NBA",
            selection     = "LeBron James points 25.5",
            sport         = "NBA",
            market_type   = "player_points",
            sportsbook    = "fanduel",
            american_odds = -130,
            recorded_at   = datetime.utcnow(),
        )
        await db.save_odds(closing)

        h, e = await _run_harvest_logic(db, datetime.utcnow(), timedelta(hours=4))
        assert h == 1
        assert e == 0


class TestSeedClvFromUdConfirmation:
    """
    Tests for Database.seed_clv_from_ud_confirmation().

    This method creates an AlertCLVSeed for an Underdog S/A alert that has
    OddsAPI market confirmation, setting bet_odds from avg_odds so the seed
    survives the harvest guard and can be CLV-graded when closing odds arrive.
    """

    NOW = datetime.utcnow()

    def _past_game(self, hours_ago: float) -> datetime:
        return datetime.utcnow() - timedelta(hours=hours_ago)

    async def test_inserts_seed_with_bet_odds(self, db):
        """A fresh call creates a seed with bet_odds populated."""
        inserted = await db.seed_clv_from_ud_confirmation(
            source_id   = 200,
            sport       = "NBA",
            stat_type   = "points",
            player_name = "LeBron James",
            line        = 25.5,
            game_time   = self._past_game(1),
            tier        = "S",
            avg_odds    = -115,
        )
        # rowcount behaviour: True when the row is created or upgraded
        fetched = await db.get_clv_seed_for_source("underdog_snapshots", 200)
        assert fetched is not None
        assert fetched.bet_odds == -115
        assert fetched.alert_type == "UNDERDOG"
        assert fetched.sport == "NBA"
        assert fetched.tier == "S"
        assert "LeBron James" in fetched.selection
        assert fetched.clv_computed is False

    async def test_upserts_bet_odds_when_existing_seed_has_none(self, db):
        """
        If seed_clv_from_ud_snapshots already created a seed with bet_odds=None,
        seed_clv_from_ud_confirmation upgrades it with the OddsAPI avg_odds.
        """
        # Simulate the periodic seed job creating a seed with bet_odds=None
        bare_seed = AlertCLVSeed(
            source_table = "underdog_snapshots",
            source_id    = 201,
            alert_type   = "UNDERDOG",
            sport        = "NBA",
            market_type  = "rebounds",
            event        = "Anthony Davis NBA",
            selection    = "Anthony Davis rebounds 12",
            bet_odds     = None,
            counterpart_odds = None,
            tier         = "A",
            game_time    = self._past_game(0.5),
            alerted_at   = datetime.utcnow(),
            clv_computed = False,
        )
        await db.save_alert_clv_seed(bare_seed)

        await db.seed_clv_from_ud_confirmation(
            source_id   = 201,
            sport       = "NBA",
            stat_type   = "rebounds",
            player_name = "Anthony Davis",
            line        = 12.0,
            game_time   = self._past_game(0.5),
            tier        = "A",
            avg_odds    = -108,
        )

        fetched = await db.get_clv_seed_for_source("underdog_snapshots", 201)
        assert fetched is not None
        assert fetched.bet_odds == -108    # upgraded from None

    async def test_does_not_overwrite_existing_bet_odds(self, db):
        """
        If a seed already has bet_odds (e.g. a second alert for the same snap),
        the existing bet_odds is preserved — on_conflict WHERE bet_odds IS NULL.
        """
        # First confirmation call — sets bet_odds = -110
        await db.seed_clv_from_ud_confirmation(
            source_id   = 202,
            sport       = "MLB",
            stat_type   = "hits",
            player_name = "Freddie Freeman",
            line        = 1.5,
            game_time   = self._past_game(1),
            tier        = "S",
            avg_odds    = -110,
        )
        # Second confirmation call with different odds — must NOT overwrite
        await db.seed_clv_from_ud_confirmation(
            source_id   = 202,
            sport       = "MLB",
            stat_type   = "hits",
            player_name = "Freddie Freeman",
            line        = 1.5,
            game_time   = self._past_game(1),
            tier        = "S",
            avg_odds    = -105,
        )

        fetched = await db.get_clv_seed_for_source("underdog_snapshots", 202)
        assert fetched is not None
        assert fetched.bet_odds == -110    # original preserved

    async def test_seed_survives_harvest_guard(self, db):
        """
        A seed created by seed_clv_from_ud_confirmation must NOT be immediately
        expired by the harvest guard — it passes the `if not seed.bet_odds` check.
        """
        await db.seed_clv_from_ud_confirmation(
            source_id   = 203,
            sport       = "NBA",
            stat_type   = "assists",
            player_name = "Nikola Jokic",
            line        = 8.5,
            game_time   = self._past_game(1),   # past game, within grace
            tier        = "A",
            avg_odds    = -120,
        )

        now   = datetime.utcnow()
        grace = timedelta(hours=4)
        h, e  = await _run_harvest_logic(db, now, grace)
        # 1h ago < 4h grace, no closing odds → left pending
        assert h == 0
        assert e == 0

        fetched = await db.get_clv_seed_for_source("underdog_snapshots", 203)
        assert fetched is not None
        assert fetched.clv_computed is False   # still pending
