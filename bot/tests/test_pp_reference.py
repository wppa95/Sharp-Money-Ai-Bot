"""
Tests for the PrizePicks reference engine (engine/pp_reference.py).

Coverage:
  - match_underdog_to_pp: exact match, normalised-stat match, no-match cases
  - Confidence scoring for each dimension (player, stat, sport, recency)
  - Confidence threshold enforcement (≥80 fires, <80 suppressed)
  - format_pp_reference_alert: content checks, disclaimer presence
  - run_pp_reference_cycle: only S/A tier props, dedup behaviour
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from engine.pp_reference import (
    match_underdog_to_pp,
    normalize_stat_for_pp,
    run_pp_reference_cycle,
    PPReferenceMatch,
    PP_SUPPORTED_SPORTS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    CONF_PLAYER_EXACT,
    CONF_STAT_NORMALISED,
    CONF_SPORT_SUPPORTED,
    CONF_RECENCY,
)
from alerts import format_pp_reference_alert, _pp_conf_bar


# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 7, 31, 12, 0, 0)
_FRESH = _NOW - timedelta(hours=1)    # within recency window
_STALE = _NOW - timedelta(hours=10)   # outside recency window


def _make_match(**kwargs) -> PPReferenceMatch:
    defaults = dict(
        player_name      = "LeBron James",
        sport            = "NBA",
        stat_type        = "points",
        ud_line          = 25.5,
        inferred_pp_line = 25.5,
        confidence       = 90,
        match_reason     = "player: LeBron James; stat: points; sport: NBA; fresh: 60m ago",
        matched_at       = _NOW,
        pp_source        = "underdog_proxy",
        pp_line_from_db  = None,
    )
    defaults.update(kwargs)
    return PPReferenceMatch(**defaults)


def _make_pp_history_row(player_name: str, sport: str, stat_type: str, line_value: float):
    """Minimal mock PropLineHistory row with provider=PrizePicks."""
    row = MagicMock()
    row.provider    = "PrizePicks"
    row.player_name = player_name
    row.sport       = sport
    row.stat_type   = stat_type
    row.line_value  = line_value
    row.fetched_at  = _FRESH
    return row


# ── normalize_stat_for_pp ─────────────────────────────────────────────────────

class TestNormalizeStatForPp:
    def test_known_abbreviation_pts(self):
        assert normalize_stat_for_pp("pts") == "points"

    def test_known_full_form_rebounds(self):
        assert normalize_stat_for_pp("rebounds") == "rebounds"

    def test_case_insensitive(self):
        assert normalize_stat_for_pp("PTS") == "points"
        assert normalize_stat_for_pp("Rebounds") == "rebounds"

    def test_strips_whitespace(self):
        assert normalize_stat_for_pp("  passing yards  ") == "passing yards"

    def test_unknown_stat_returns_lowercase_raw(self):
        assert normalize_stat_for_pp("mystery_stat") == "mystery_stat"

    def test_fantasy_points(self):
        assert normalize_stat_for_pp("fantasy points") == "fantasy points"

    def test_kills_esports(self):
        assert normalize_stat_for_pp("kills") == "kills"


# ── match_underdog_to_pp — core confidence scoring ────────────────────────────

class TestMatchConfidenceScoring:
    """Verify that each dimension contributes the right number of points."""

    def test_full_confidence_all_dimensions(self):
        """Clean player + known stat + supported sport + fresh data = 100."""
        result = match_underdog_to_pp(
            player_name = "Patrick Mahomes",
            sport       = "NFL",
            stat_type   = "passing yards",
            line_value  = 285.5,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is not None
        assert result.confidence == 100

    def test_stale_data_loses_recency_points(self):
        """Stale data (>6h) drops 10 pts; 90 >= threshold so still matches."""
        result = match_underdog_to_pp(
            player_name = "Patrick Mahomes",
            sport       = "NFL",
            stat_type   = "passing yards",
            line_value  = 285.5,
            fetched_at  = _STALE,
            now         = _NOW,
        )
        assert result is not None
        expected = CONF_PLAYER_EXACT + CONF_STAT_NORMALISED + CONF_SPORT_SUPPORTED
        assert result.confidence == expected   # 90

    def test_unknown_stat_loses_stat_points(self):
        """Unmapped stat drops 30 pts: 40+20+10 = 70 < threshold → None."""
        result = match_underdog_to_pp(
            player_name = "Patrick Mahomes",
            sport       = "NFL",
            stat_type   = "mystery_special_stat",
            line_value  = 10.0,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is None

    def test_unsupported_sport_loses_sport_points(self):
        """Unsupported sport drops 20 pts: 40+30+10 = 80 = exactly threshold → matches."""
        result = match_underdog_to_pp(
            player_name = "Some Player",
            sport       = "GOLF",     # not in PP_SUPPORTED_SPORTS
            stat_type   = "points",
            line_value  = 5.0,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        # 40 (player) + 30 (stat) + 0 (sport) + 10 (recency) = 80 → at threshold
        assert result is not None
        assert result.confidence == 80

    def test_unsupported_sport_stale_below_threshold(self):
        """Unsupported sport + stale = 40+30+0+0 = 70 → below threshold → None."""
        result = match_underdog_to_pp(
            player_name = "Some Player",
            sport       = "GOLF",
            stat_type   = "points",
            line_value  = 5.0,
            fetched_at  = _STALE,
            now         = _NOW,
        )
        assert result is None

    def test_empty_player_name_below_threshold(self):
        """Empty player name loses 40 pts → 0+30+20+10 = 60 → None."""
        result = match_underdog_to_pp(
            player_name = "",
            sport       = "NBA",
            stat_type   = "points",
            line_value  = 20.0,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is None

    def test_unknown_player_name_below_threshold(self):
        """'Unknown' player name loses 40 pts → None."""
        result = match_underdog_to_pp(
            player_name = "unknown",
            sport       = "NBA",
            stat_type   = "points",
            line_value  = 20.0,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is None

    def test_no_fetched_at_credits_recency(self):
        """None fetched_at gets recency benefit-of-the-doubt (+10)."""
        result = match_underdog_to_pp(
            player_name = "LeBron James",
            sport       = "NBA",
            stat_type   = "points",
            line_value  = 25.5,
            fetched_at  = None,
            now         = _NOW,
        )
        assert result is not None
        assert result.confidence == 100


# ── match_underdog_to_pp — match content ─────────────────────────────────────

class TestMatchContent:
    def test_ud_line_is_proxy_pp_line_when_no_db_rows(self):
        result = match_underdog_to_pp(
            player_name = "Shohei Ohtani",
            sport       = "MLB",
            stat_type   = "hits",
            line_value  = 1.5,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is not None
        assert result.ud_line == 1.5
        assert result.inferred_pp_line == 1.5
        assert result.pp_source == "underdog_proxy"
        assert result.pp_line_from_db is None

    def test_stat_type_normalised_in_result(self):
        result = match_underdog_to_pp(
            player_name = "Nikola Jokic",
            sport       = "NBA",
            stat_type   = "reb",    # abbreviated
            line_value  = 12.5,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is not None
        assert result.stat_type == "rebounds"

    def test_player_name_preserved_in_result(self):
        result = match_underdog_to_pp(
            player_name = "  Nikola Jokic  ",  # extra whitespace
            sport       = "NBA",
            stat_type   = "rebounds",
            line_value  = 12.5,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is not None
        assert result.player_name == "Nikola Jokic"

    def test_match_reason_contains_key_labels(self):
        result = match_underdog_to_pp(
            player_name = "Steph Curry",
            sport       = "NBA",
            stat_type   = "pts",
            line_value  = 29.5,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is not None
        assert "Steph Curry" in result.match_reason
        assert "points"      in result.match_reason
        assert "NBA"         in result.match_reason

    def test_matched_at_set_to_now(self):
        result = match_underdog_to_pp(
            player_name = "Kevin Durant",
            sport       = "NBA",
            stat_type   = "points",
            line_value  = 28.0,
            fetched_at  = _FRESH,
            now         = _NOW,
        )
        assert result is not None
        assert result.matched_at == _NOW


# ── match_underdog_to_pp — PropLineHistory cross-reference ────────────────────

class TestPropHistoryMatch:
    def test_pp_row_match_sets_source_to_prop_history_match(self):
        pp_row = _make_pp_history_row("LeBron James", "NBA", "points", 26.0)
        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "points",
            line_value        = 25.5,
            fetched_at        = _FRESH,
            prop_history_rows = [pp_row],
            now               = _NOW,
        )
        assert result is not None
        assert result.pp_source == "prop_history_match"
        assert result.pp_line_from_db == 26.0
        assert result.inferred_pp_line == 26.0   # uses DB line, not UD line

    def test_different_player_pp_row_does_not_match(self):
        pp_row = _make_pp_history_row("Anthony Davis", "NBA", "points", 24.0)
        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "points",
            line_value        = 25.5,
            fetched_at        = _FRESH,
            prop_history_rows = [pp_row],
            now               = _NOW,
        )
        # No PP row match → falls back to underdog_proxy
        assert result is not None
        assert result.pp_source == "underdog_proxy"

    def test_non_pp_rows_ignored(self):
        """Rows with provider='Underdog' must not be counted as PP matches."""
        ud_row = _make_pp_history_row("LeBron James", "NBA", "points", 25.0)
        ud_row.provider = "Underdog"
        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "points",
            line_value        = 25.5,
            fetched_at        = _FRESH,
            prop_history_rows = [ud_row],
            now               = _NOW,
        )
        assert result is not None
        assert result.pp_source == "underdog_proxy"

    def test_case_insensitive_player_match(self):
        pp_row = _make_pp_history_row("lebron james", "NBA", "points", 26.0)
        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "points",
            line_value        = 25.5,
            fetched_at        = _FRESH,
            prop_history_rows = [pp_row],
            now               = _NOW,
        )
        assert result is not None
        assert result.pp_source == "prop_history_match"

    def test_different_stat_type_pp_row_not_matched(self):
        """
        Same player/sport but DIFFERENT stat (points vs rebounds) must NOT
        produce a prop_history_match — cross-attaching a points line to a
        rebounds prop would display a materially inaccurate inferred PP line.
        """
        pp_row = _make_pp_history_row("LeBron James", "NBA", "points", 26.0)
        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "rebounds",   # different stat
            line_value        = 8.5,
            fetched_at        = _FRESH,
            prop_history_rows = [pp_row],
            now               = _NOW,
        )
        # Should fall back to underdog_proxy — NOT prop_history_match
        assert result is not None
        assert result.pp_source == "underdog_proxy", (
            "A PP row for 'points' must not be matched to a 'rebounds' prop"
        )
        assert result.pp_line_from_db is None
        # The inferred line must be the UD line, not the PP points line (26.0)
        assert result.inferred_pp_line == 8.5

    def test_stat_alias_pp_row_matched_via_normalisation(self):
        """
        'pts' (Underdog abbreviation) and 'points' (PP canonical) normalise to
        the same canonical stat and MUST produce a prop_history_match.
        """
        pp_row = _make_pp_history_row("LeBron James", "NBA", "points", 26.0)
        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "pts",   # UD abbreviation → normalises to "points"
            line_value        = 25.5,
            fetched_at        = _FRESH,
            prop_history_rows = [pp_row],
            now               = _NOW,
        )
        assert result is not None
        assert result.pp_source == "prop_history_match", (
            "'pts' should normalise to 'points' and match the PP row"
        )
        assert result.pp_line_from_db == 26.0

    def test_newest_pp_row_selected_when_multiple_match(self):
        """When multiple PP rows match player/sport/stat, the newest is used."""
        older_row = _make_pp_history_row("LeBron James", "NBA", "points", 24.0)
        older_row.fetched_at = _NOW - timedelta(hours=8)
        newer_row = _make_pp_history_row("LeBron James", "NBA", "points", 26.5)
        newer_row.fetched_at = _NOW - timedelta(hours=1)

        result = match_underdog_to_pp(
            player_name       = "LeBron James",
            sport             = "NBA",
            stat_type         = "points",
            line_value        = 25.5,
            fetched_at        = _FRESH,
            prop_history_rows = [older_row, newer_row],
            now               = _NOW,
        )
        assert result is not None
        assert result.pp_line_from_db == 26.5, (
            "The newer PP row (26.5) should be preferred over the older one (24.0)"
        )


# ── format_pp_reference_alert ─────────────────────────────────────────────────

class TestFormatPpReferenceAlert:
    def test_purple_emoji_prefix(self):
        msg = format_pp_reference_alert(_make_match())
        assert msg.startswith("🟣")

    def test_contains_mandatory_disclaimer(self):
        msg = format_pp_reference_alert(_make_match())
        assert "Reference only" in msg
        assert "not confirmed PrizePicks data" in msg

    def test_contains_player_name(self):
        msg = format_pp_reference_alert(_make_match(player_name="LeBron James"))
        assert "LeBron James" in msg

    def test_contains_ud_line(self):
        msg = format_pp_reference_alert(_make_match(ud_line=25.5))
        assert "25.5" in msg

    def test_contains_confidence_score(self):
        msg = format_pp_reference_alert(_make_match(confidence=90))
        assert "90" in msg

    def test_contains_sport(self):
        msg = format_pp_reference_alert(_make_match(sport="NBA"))
        assert "NBA" in msg

    def test_contains_stat_type(self):
        msg = format_pp_reference_alert(_make_match(stat_type="points"))
        assert "points" in msg

    def test_plus_minus_note_in_proxy_source(self):
        msg = format_pp_reference_alert(_make_match(pp_source="underdog_proxy"))
        assert "±0.5" in msg

    def test_pp_history_match_source_label(self):
        match = _make_match(
            pp_source="prop_history_match",
            pp_line_from_db=26.0,
            inferred_pp_line=26.0,
        )
        msg = format_pp_reference_alert(match)
        assert "PP History Match" in msg

    def test_alert_is_html_safe_string(self):
        """Alert must be a non-empty string (HTML for Telegram)."""
        msg = format_pp_reference_alert(_make_match())
        assert isinstance(msg, str)
        assert len(msg) > 50

    def test_utc_timestamp_present(self):
        msg = format_pp_reference_alert(_make_match(matched_at=_NOW))
        assert "UTC" in msg

    def test_conf_bar_full(self):
        bar = _pp_conf_bar(100)
        assert "██████████" in bar

    def test_conf_bar_empty(self):
        bar = _pp_conf_bar(0)
        assert "░░░░░░░░░░" in bar

    def test_conf_bar_partial(self):
        bar = _pp_conf_bar(50)
        assert "█████" in bar
        assert "░░░░░" in bar


# ── run_pp_reference_cycle ────────────────────────────────────────────────────

class TestRunPpReferenceCycle:
    """Test the batch cycle helper that wires into underdog_job."""

    def _make_db(self, pp_rows=None):
        db = MagicMock()
        db.get_latest_props_for_provider = AsyncMock(return_value=pp_rows or [])
        return db

    def _make_bot(self):
        bot = MagicMock()
        return bot

    def _make_scored_prop(
        self,
        player="LeBron James",
        sport="NBA",
        stat_type="points",
        line=25.5,
        tier="A",
    ) -> dict:
        return {
            "player":    player,
            "sport":     sport,
            "stat_type": stat_type,
            "line":      line,
            "tier":      tier,
            "total":     85,
        }

    @pytest.mark.asyncio
    async def test_s_tier_prop_triggers_reference_alert(self):
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}):
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [self._make_scored_prop(tier="S")],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 1

    @pytest.mark.asyncio
    async def test_a_tier_prop_triggers_reference_alert(self):
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}):
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [self._make_scored_prop(tier="A")],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 1

    @pytest.mark.asyncio
    async def test_b_tier_prop_not_sent(self):
        """B-tier props are below the quality bar for PP reference alerts."""
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}) as mock_bc:
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [self._make_scored_prop(tier="B")],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 0
        mock_bc.assert_not_called()

    @pytest.mark.asyncio
    async def test_pass_tier_prop_not_sent(self):
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}) as mock_bc:
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [self._make_scored_prop(tier="PASS")],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 0

    @pytest.mark.asyncio
    async def test_dedup_prevents_second_alert_same_prop_line(self):
        """Same prop/line within dedup window → no second alert."""
        import time as _time
        db   = self._make_db()
        bot  = self._make_bot()
        prop = self._make_scored_prop(tier="S")
        # Pre-populate dict dedup: key=(player, sport, stat), value=(ts, line)
        # Recent alert (10 s ago), same line (25.5) → should be deduped
        seen: dict = {("LeBron James", "NBA", "points"): (_time.time() - 10, 25.5)}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}) as mock_bc:
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [prop],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 0
        mock_bc.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_line_fires_after_old_line_deduped(self):
        """Significant line move (≥ MIN_CHANGE) → fires even within window."""
        import time as _time
        db   = self._make_db()
        bot  = self._make_bot()
        # Old alert at line 25.5 (10 s ago); new line 26.0 — delta=0.5 ≥ MIN_CHANGE
        seen: dict = {("LeBron James", "NBA", "points"): (_time.time() - 10, 25.5)}
        prop = self._make_scored_prop(tier="A", line=26.0)

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}):
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [prop],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 1

    @pytest.mark.asyncio
    async def test_dedup_key_added_after_send(self):
        """Successful send populates alerted_set so next call is deduped."""
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}
        prop = self._make_scored_prop(tier="S")

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}):
            await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [prop],
                alerted_set  = seen,
                now          = _NOW,
            )

        # New key format: (player, sport, stat) → (timestamp, line)
        assert ("LeBron James", "NBA", "points") in seen

    @pytest.mark.asyncio
    async def test_empty_scored_props_returns_zero(self):
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        count = await run_pp_reference_cycle(
            db           = db,
            bot          = bot,
            chat_ids     = [123],
            scored_props = [],
            alerted_set  = seen,
            now          = _NOW,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_no_high_tier_props_returns_zero(self):
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        props = [
            self._make_scored_prop(tier="B"),
            self._make_scored_prop(tier="PASS"),
        ]

        count = await run_pp_reference_cycle(
            db           = db,
            bot          = bot,
            chat_ids     = [123],
            scored_props = props,
            alerted_set  = seen,
            now          = _NOW,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_raise(self):
        """broadcast_alert raising should be caught and not propagate."""
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, side_effect=RuntimeError("network error")):
            # Must not raise
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [self._make_scored_prop(tier="S")],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 0
        # Nothing added to dedup dict on failure
        assert len(seen) == 0

    @pytest.mark.asyncio
    async def test_db_failure_continues_gracefully(self):
        """DB failure fetching PP rows is non-fatal; engine falls back to proxy mode."""
        db = self._make_db()
        db.get_latest_props_for_provider = AsyncMock(side_effect=RuntimeError("DB error"))
        bot  = self._make_bot()
        seen: dict = {}

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}):
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [self._make_scored_prop(tier="A")],
                alerted_set  = seen,
                now          = _NOW,
            )

        # Should still send as underdog_proxy
        assert count == 1

    @pytest.mark.asyncio
    async def test_multiple_props_multiple_alerts(self):
        """Multiple S/A tier props each trigger their own reference alert."""
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        props = [
            self._make_scored_prop(player="LeBron James", tier="S", line=25.5),
            self._make_scored_prop(player="Steph Curry",  tier="A", line=29.5, stat_type="points"),
        ]

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}):
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = props,
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 2

    @pytest.mark.asyncio
    async def test_low_confidence_prop_not_sent(self):
        """Prop with unsupported sport and stale data (conf=70) → no alert."""
        db   = self._make_db()
        bot  = self._make_bot()
        seen: dict = {}

        prop = {
            "player":    "Tiger Woods",
            "sport":     "GOLF",    # not in PP_SUPPORTED_SPORTS → -20
            "stat_type": "mystery", # unmapped stat → -30
            "line":      3.5,
            "tier":      "S",
            "total":     90,
        }

        with patch("alerts.broadcast_alert", new_callable=AsyncMock, return_value={"sent": 1, "failed": 0}) as mock_bc:
            count = await run_pp_reference_cycle(
                db           = db,
                bot          = bot,
                chat_ids     = [123],
                scored_props = [prop],
                alerted_set  = seen,
                now          = _NOW,
            )

        assert count == 0
        mock_bc.assert_not_called()
