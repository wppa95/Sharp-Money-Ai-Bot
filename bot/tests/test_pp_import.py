"""
Tests for the /pp_import command and PrizePicks manual data flow.

Tests:
1. Parsing logic for the pipe-delimited import format
2. Full lifecycle via upsert_prop_line_lifecycle (ADDED / CHANGED / REMOVED / RETURNED)
3. Provider isolation (PrizePicks records don't pollute Underdog history)
4. Error handling for malformed input
"""

from __future__ import annotations

import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import Database, PropLineHistory

# ── Shared event loop ─────────────────────────────────────────────────────────





# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


# ── Parse helper (extracted from cmd_pp_import logic) ─────────────────────────

def _parse_pp_import_line(raw: str) -> Optional[dict]:
    """
    Parse one pipe-delimited import line.
    Returns None on error.
    Format: PLAYER | STAT | LINE | SPORT [| removed]
    """
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        return None
    player_name = parts[0]
    stat_type   = parts[1]
    sport       = parts[3].upper()
    try:
        line_value = float(parts[2].replace(",", ""))
    except ValueError:
        return None
    is_removed = len(parts) >= 5 and "removed" in parts[4].lower()
    return {
        "player_name": player_name,
        "stat_type":   stat_type,
        "line_value":  line_value,
        "sport":       sport,
        "removed":     is_removed,
    }


async def _import_props(db: Database, lines: list[str]) -> list[tuple[str, str, str]]:
    """Import a list of pipe-delimited lines and return (player, stat, event) tuples."""
    results = []
    for raw in lines:
        parsed = _parse_pp_import_line(raw)
        if parsed is None:
            continue
        _, event = await db.upsert_prop_line_lifecycle(
            provider    = "PrizePicks",
            player_name = parsed["player_name"],
            sport       = parsed["sport"],
            stat_type   = parsed["stat_type"],
            line_value  = parsed["line_value"],
            removed     = parsed["removed"],
            fetched_at  = datetime.utcnow(),
        )
        results.append((parsed["player_name"], parsed["stat_type"], event))
    return results


@pytest.mark.asyncio
async def test_active_fpr_pool_returns_fresh_mixed_provider_lines(db):
    """The canonical FPR pool includes both provider namespaces."""
    now = datetime.utcnow()
    await db.upsert_prop_line_history_batch(
        "PrizePicks",
        [SimpleNamespace(
            player_name="PP Player",
            sport="NBA",
            stat_type="Points",
            line_value=18.5,
            external_id="pp-1",
            event_id="pp-game-1",
            team="DET",
            game_time=now + timedelta(hours=2),
            source_timestamp=now,
        )],
    )
    await db.upsert_prop_line_history_batch(
        "Underdog",
        [SimpleNamespace(
            player_name="UD Player",
            sport="MLB",
            stat_type="Hits",
            line_value=1.5,
            external_id="ud-1",
            event_id="ud-game-1",
            team="NYY",
            game_time=now + timedelta(hours=2),
            source_timestamp=now,
        )],
    )

    pool = await db.get_all_active_prop_line_history_by_line()

    assert ("PrizePicks", "PP Player", "NBA", "Points", 18.5, "pp-1") in pool
    assert ("Underdog", "UD Player", "MLB", "Hits", 1.5, "ud-1") in pool


@pytest.mark.asyncio
async def test_pp_source_ids_keep_same_event_alternate_lines_separate(db):
    """PP projections sharing an event still represent separate active lines."""
    now = datetime.utcnow()
    props = [
        SimpleNamespace(
            player_name="PP Player",
            sport="NBA",
            stat_type="Points",
            line_value=18.5,
            external_id="pp-low",
            event_id="pp-game-1",
            team="DET",
            game_time=now + timedelta(hours=2),
            source_timestamp=now,
        ),
        SimpleNamespace(
            player_name="PP Player",
            sport="NBA",
            stat_type="Points",
            line_value=20.5,
            external_id="pp-high",
            event_id="pp-game-1",
            team="DET",
            game_time=now + timedelta(hours=2),
            source_timestamp=now,
        ),
    ]

    await db.upsert_prop_line_history_batch("PrizePicks", props)
    pool = await db.get_all_active_prop_line_history_by_line(
        providers=("PrizePicks",)
    )

    assert set(pool) == {
        ("PrizePicks", "PP Player", "NBA", "Points", 18.5, "pp-low"),
        ("PrizePicks", "PP Player", "NBA", "Points", 20.5, "pp-high"),
    }


@pytest.mark.asyncio
async def test_pp_source_id_line_move_updates_existing_active_projection(db):
    """A moved PP source ID updates its row rather than creating a duplicate."""
    now = datetime.utcnow()
    prop = SimpleNamespace(
        player_name="PP Player",
        sport="NBA",
        stat_type="Points",
        line_value=18.5,
        external_id="pp-source-1",
        event_id="pp-game-1",
        team="DET",
        game_time=now + timedelta(hours=2),
        source_timestamp=now,
    )
    await db.upsert_prop_line_history_batch("PrizePicks", [prop])
    prop.line_value = 19.5
    prop.source_timestamp = now + timedelta(minutes=1)
    await db.upsert_prop_line_history_batch("PrizePicks", [prop])

    pool = await db.get_all_active_prop_line_history_by_line(
        providers=("PrizePicks",)
    )
    assert set(pool) == {
        ("PrizePicks", "PP Player", "NBA", "Points", 19.5, "pp-source-1")
    }


@pytest.mark.asyncio
async def test_current_pool_ignores_historical_rows_but_history_remains_queryable(db):
    """FPR reads today's feed only while grading history stays append-only."""
    from reporting import current_reporting_day_bounds

    today_start, _today_end = current_reporting_day_bounds()
    now = today_start + timedelta(hours=2)
    old_row = PropLineHistory(
        provider="PrizePicks",
        sport="NBA",
        player_name="Historical Player",
        team="DET",
        stat_type="Points",
        line_value=18.5,
        external_id="pp-history-1",
        game_id="pp-game-1",
        game_time=now + timedelta(hours=2),
        fetched_at=today_start - timedelta(days=1),
        first_seen=today_start - timedelta(days=1),
        last_seen=today_start - timedelta(days=1),
        removed=False,
    )
    await db.save_prop_line_history(old_row)

    current_prop = SimpleNamespace(
        player_name="Historical Player",
        sport="NBA",
        stat_type="Points",
        line_value=19.5,
        external_id="pp-history-1",
        event_id="pp-game-1",
        team="DET",
        game_time=now + timedelta(hours=2),
        source_timestamp=now,
    )
    await db.upsert_prop_line_history_batch(
        "PrizePicks", [current_prop], append_snapshot=True
    )

    pool = await db.get_all_active_prop_line_history_by_line(
        providers=("PrizePicks",)
    )
    assert set(pool) == {
        ("PrizePicks", "Historical Player", "NBA", "Points", 19.5, "pp-history-1")
    }

    history = await db.get_prop_line_history(
        "PrizePicks",
        "Historical Player",
        "NBA",
        "Points",
        external_id="pp-history-1",
    )
    assert len(history) == 2
    assert {row.line_value for row in history} == {18.5, 19.5}


@pytest.mark.asyncio
async def test_current_pool_removal_marker_wins_over_older_active_line(db):
    """A same-day removal marker cannot let an older line re-enter the pool."""
    from reporting import current_reporting_day_bounds

    today_start, _today_end = current_reporting_day_bounds()
    now = today_start + timedelta(hours=2)
    identity = dict(
        provider="PrizePicks",
        sport="NBA",
        player_name="Removed Player",
        team="DET",
        stat_type="Points",
        line_value=20.5,
        external_id="pp-removed-1",
        game_id="pp-game-2",
        game_time=now + timedelta(hours=2),
    )
    await db.save_prop_line_history(
        PropLineHistory(**identity, fetched_at=now, removed=False)
    )
    await db.save_prop_line_history(
        PropLineHistory(
            **identity,
            fetched_at=now + timedelta(minutes=1),
            removed=True,
        )
    )

    pool = await db.get_all_active_prop_line_history_by_line(
        providers=("PrizePicks",)
    )
    assert not pool


# ── Parse tests ───────────────────────────────────────────────────────────────

class TestParsePPImportLine:
    async def test_basic_line(self):
        r = _parse_pp_import_line("LeBron James | Points | 25.5 | NBA")
        assert r["player_name"] == "LeBron James"
        assert r["stat_type"]   == "Points"
        assert r["line_value"]  == 25.5
        assert r["sport"]       == "NBA"
        assert r["removed"]     is False

    async def test_removed_marker(self):
        r = _parse_pp_import_line("Mike Trout | Hits | 1.5 | MLB | removed")
        assert r["removed"] is True

    async def test_removed_case_insensitive(self):
        r = _parse_pp_import_line("Mike Trout | Hits | 1.5 | MLB | REMOVED")
        assert r["removed"] is True

    async def test_sport_uppercased(self):
        r = _parse_pp_import_line("LeBron | Points | 25.5 | nba")
        assert r["sport"] == "NBA"

    async def test_too_few_fields_returns_none(self):
        assert _parse_pp_import_line("LeBron | Points | 25.5") is None

    async def test_invalid_line_value_returns_none(self):
        assert _parse_pp_import_line("LeBron | Points | bad | NBA") is None

    async def test_comma_in_line_value_stripped(self):
        r = _parse_pp_import_line("Patrick Mahomes | Pass Yards | 275,5 | NFL")
        assert r["line_value"] == 2755.0  # comma stripped → 2755 (not decimal sep)

    async def test_empty_string_returns_none(self):
        assert _parse_pp_import_line("") is None

    async def test_whitespace_stripped(self):
        r = _parse_pp_import_line("  LeBron James  |  Points  |  25.5  |  NBA  ")
        assert r["player_name"] == "LeBron James"
        assert r["stat_type"]   == "Points"

    async def test_float_line_value(self):
        r = _parse_pp_import_line("LeBron | Points | 25.5 | NBA")
        assert isinstance(r["line_value"], float)


# ── End-to-end import lifecycle tests ────────────────────────────────────────

class TestPPImportLifecycle:
    async def test_single_import_added(self, db):
        results = await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])
        assert len(results) == 1
        assert results[0][2] == "ADDED"

    async def test_second_import_same_line_unchanged(self, db):
        await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])
        results = await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])
        assert results[0][2] == "UNCHANGED"

    async def test_line_change_detected(self, db):
        await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])
        results = await _import_props(db, ["LeBron James | Points | 27.5 | NBA"])
        assert results[0][2] == "CHANGED"

    async def test_removed_lifecycle(self, db):
        await _import_props(db, ["Mike Trout | Hits | 1.5 | MLB"])
        results = await _import_props(db, ["Mike Trout | Hits | 1.5 | MLB | removed"])
        assert results[0][2] == "REMOVED"

    async def test_returned_after_removed(self, db):
        await _import_props(db, ["Mike Trout | Hits | 1.5 | MLB"])
        await _import_props(db, ["Mike Trout | Hits | 1.5 | MLB | removed"])
        results = await _import_props(db, ["Mike Trout | Hits | 1.5 | MLB"])
        assert results[0][2] == "RETURNED"

    async def test_multi_prop_import(self, db):
        lines = [
            "LeBron James | Points | 25.5 | NBA",
            "Mike Trout | Hits | 1.5 | MLB",
            "Patrick Mahomes | Pass Yards | 275.5 | NFL",
        ]
        results = await _import_props(db, lines)
        assert len(results) == 3
        assert all(r[2] == "ADDED" for r in results)

    async def test_malformed_lines_skipped(self, db):
        lines = [
            "bad line without pipes",
            "LeBron James | Points | 25.5 | NBA",
            "only | two",
        ]
        results = await _import_props(db, lines)
        assert len(results) == 1  # only the valid line
        assert results[0][2] == "ADDED"

    async def test_pp_does_not_pollute_underdog_history(self, db):
        await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])

        pp_count = await db.count_prop_line_history(provider="PrizePicks")
        ud_count = await db.count_prop_line_history(provider="Underdog")
        assert pp_count >= 1
        assert ud_count == 0

    async def test_imported_props_stored_correctly(self, db):
        await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])

        rows = await db.get_prop_line_history(
            "PrizePicks", "LeBron James", "NBA", "Points"
        )
        assert len(rows) >= 1
        row = rows[0]
        assert row.player_name == "LeBron James"
        assert row.stat_type   == "Points"
        assert abs(row.line_value - 25.5) < 1e-6
        assert row.provider    == "PrizePicks"

    async def test_line_change_stores_updated_value(self, db):
        await _import_props(db, ["LeBron James | Points | 25.5 | NBA"])
        await _import_props(db, ["LeBron James | Points | 27.5 | NBA"])

        rows = await db.get_prop_line_history(
            "PrizePicks", "LeBron James", "NBA", "Points"
        )
        assert len(rows) >= 1
        # Most-recent row should have the new line value
        latest = rows[0]
        assert abs(latest.line_value - 27.5) < 1e-6

    async def test_multiple_stats_for_same_player_isolated(self, db):
        """Points and Rebounds for LeBron should be independent lifecycle entries."""
        lines = [
            "LeBron James | Points | 25.5 | NBA",
            "LeBron James | Rebounds | 7.5 | NBA",
        ]
        results = await _import_props(db, lines)
        assert all(r[2] == "ADDED" for r in results)
        assert len(results) == 2

    async def test_import_count_matches_expected(self, db):
        lines = [
            "LeBron James | Points | 25.5 | NBA",
            "Mike Trout | Hits | 1.5 | MLB",
        ]
        await _import_props(db, lines)
        total = await db.count_prop_line_history(provider="PrizePicks")
        assert total == 2
