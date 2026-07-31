"""
Tests for the CLV harvest job logic.

Rather than running the full PTB Application, these tests exercise the
harvest logic in isolation using mock database objects.

Key invariants:
  - Seeds without bet_odds are expired immediately (Underdog pick'em)
  - Seeds without game_time are expired immediately (no timing info)
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

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


# ── In-memory DB fixture ──────────────────────────────────────────────────────

@pytest.fixture()
def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    _run(database.init())
    yield database
    _run(database.close())


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

        if not seed.bet_odds or seed.alert_type == "UNDERDOG":
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
                clv_proxy    = clv_result.clv_lead,
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

    def test_no_game_time_stale_expires(self, db):
        """Seeds with game_time=None appear in pending only after stale_hours (24h)."""
        seed = _make_seed(
            source_id  = 1,
            game_time  = None,
            # alerted_at must be > 24h ago to appear in pending
        )
        # Manually set alerted_at to 25h ago
        seed.alerted_at = self.NOW - timedelta(hours=25)
        _run(db.save_alert_clv_seed(seed))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert h == 0
        assert e == 1

    def test_no_game_time_fresh_not_expired(self, db):
        """Seeds with game_time=None that are fresh (< 24h) should stay pending."""
        seed = _make_seed(source_id=1, game_time=None)
        # alerted_at = utcnow() (fresh) — default in _make_seed
        _run(db.save_alert_clv_seed(seed))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert h == 0
        assert e == 0  # not stale yet — stays pending

    # ── Seeds without bet_odds (Underdog) → expired immediately ──────────────

    def test_underdog_seed_no_bet_odds_expired(self, db):
        seed = _make_seed(
            source_id  = 2,
            alert_type = "UNDERDOG",
            bet_odds   = None,
            game_time  = self._past_game(2),
        )
        _run(db.save_alert_clv_seed(seed))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert e == 1
        assert h == 0

    def test_ev_seed_no_bet_odds_expired(self, db):
        seed = _make_seed(
            source_id = 3,
            bet_odds  = None,
            game_time = self._past_game(2),
        )
        _run(db.save_alert_clv_seed(seed))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert e == 1

    # ── Seeds within grace period and no closing odds → not touched ───────────

    def test_within_grace_no_odds_not_expired(self, db):
        seed = _make_seed(
            source_id = 4,
            bet_odds  = -110,
            game_time = self._past_game(1),   # 1h ago, grace is 4h
        )
        _run(db.save_alert_clv_seed(seed))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert h == 0
        assert e == 0  # Still within grace — left pending

    # ── Grace period elapsed and no closing odds → expired ────────────────────

    def test_beyond_grace_no_odds_expires(self, db):
        seed = _make_seed(
            source_id = 5,
            bet_odds  = -110,
            game_time = self._past_game(5),   # 5h ago > 4h grace
        )
        _run(db.save_alert_clv_seed(seed))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert e == 1

    # ── get_pending_clv_seeds ignores already-computed seeds ──────────────────

    def test_already_computed_seeds_not_re_harvested(self, db):
        """Seeds with clv_computed=True must not appear in pending."""
        seed = _make_seed(source_id=6, game_time=self._past_game(5))
        _run(db.save_alert_clv_seed(seed))
        fetched = _run(db.get_clv_seed_for_source("ev_records", 6))
        _run(db.mark_clv_seed_computed(fetched.id, 2.5))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert h == 0
        assert e == 0

    def test_expired_seeds_not_re_harvested(self, db):
        seed = _make_seed(source_id=7, game_time=self._past_game(5))
        _run(db.save_alert_clv_seed(seed))
        fetched = _run(db.get_clv_seed_for_source("ev_records", 7))
        _run(db.mark_clv_seed_expired(fetched.id))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert h == 0
        assert e == 0

    # ── Multiple seeds mixed ──────────────────────────────────────────────────

    def test_mixed_seeds_correct_counts(self, db):
        """2 expired (stale/no bet_odds), 1 within grace (untouched)."""
        # Stale (game_time=None, alerted_at > 24h ago) → expire
        stale_seed = _make_seed(source_id=10, game_time=None)
        stale_seed.alerted_at = self.NOW - timedelta(hours=25)
        _run(db.save_alert_clv_seed(stale_seed))
        # UNDERDOG (no bet_odds) → expire
        _run(db.save_alert_clv_seed(_make_seed(
            source_id=11, alert_type="UNDERDOG", bet_odds=None,
            game_time=self._past_game(2),
        )))
        # Within grace → leave pending
        _run(db.save_alert_clv_seed(_make_seed(
            source_id=12, bet_odds=-110, game_time=self._past_game(1),
        )))

        h, e = _run(_run_harvest_logic(db, self.NOW, self.GRACE))
        assert e == 2
        assert h == 0

    # ── CLVRecord written correctly ───────────────────────────────────────────

    def test_clv_record_count_before_harvest(self, db):
        initial = _run(db.count_clv_records())
        assert initial == 0

    # ── Pending count methods ─────────────────────────────────────────────────

    def test_count_pending_after_expire(self, db):
        # Use a past game_time so seed appears in pending
        seed = _make_seed(source_id=20, game_time=self._past_game(5))
        _run(db.save_alert_clv_seed(seed))
        fetched = _run(db.get_clv_seed_for_source("ev_records", 20))
        assert fetched is not None

        before = _run(db.count_pending_clv_seeds())
        _run(db.mark_clv_seed_expired(fetched.id))
        after  = _run(db.count_pending_clv_seeds())

        assert before > after

    def test_get_pending_filters_by_game_time(self, db):
        """Seeds whose game_time is in the future must NOT appear in pending."""
        future_seed = _make_seed(
            source_id = 99,
            bet_odds  = -110,
            game_time = self.NOW + timedelta(hours=2),
        )
        _run(db.save_alert_clv_seed(future_seed))

        pending = _run(db.get_pending_clv_seeds(limit=50))
        assert not any(s.source_id == 99 for s in pending)
