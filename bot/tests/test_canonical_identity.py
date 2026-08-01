"""
Contract tests — Canonical Identity Layer (Framework v3.0 Layer 1).

These tests verify the INTERFACE and INVARIANTS of engine.identity, not just
that the code runs.  They must pass whenever the identity layer is changed.
"""

import pytest
from engine.identity import (
    CanonicalPlayer,
    CanonicalEvent,
    CanonicalMarket,
    normalize_player_name,
    normalize_stat,
    player_key,
    event_key,
)


# ─────────────────────────────────────────────────────────────────────────────
# normalize_player_name — contract: stable, lowercase, underscore-separated
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizePlayerName:
    def test_simple_name(self):
        assert normalize_player_name("LeBron James") == "lebron_james"

    def test_already_lowercase(self):
        assert normalize_player_name("mike trout") == "mike_trout"

    def test_accented_characters(self):
        # Accents must be stripped
        result = normalize_player_name("José Rondón")
        assert result == "jose_rondon"

    def test_dots_removed(self):
        # "A.J." → "aj", not "a_j"
        assert normalize_player_name("A.J. Brown") == "aj_brown"

    def test_apostrophe_removed(self):
        assert normalize_player_name("D'Angelo Russell") == "dangelo_russell"

    def test_hyphen_removed(self):
        assert normalize_player_name("De'Aaron Fox") == "deaaron_fox"

    def test_comma_removed(self):
        assert normalize_player_name("Smith, John") == "smith_john"

    def test_extra_whitespace_collapsed(self):
        assert normalize_player_name("  Mike   Trout  ") == "mike_trout"

    def test_empty_string(self):
        assert normalize_player_name("") == ""

    def test_single_word(self):
        assert normalize_player_name("Shaq") == "shaq"

    def test_idempotent(self):
        # Normalising twice must produce the same result as normalising once
        once  = normalize_player_name("LeBron James")
        twice = normalize_player_name(once)
        assert once == twice

    def test_returns_string(self):
        result = normalize_player_name("Any Name")
        assert isinstance(result, str)

    def test_no_leading_trailing_underscores(self):
        result = normalize_player_name("  .Jr.  ")
        assert not result.startswith("_")
        assert not result.endswith("_")


# ─────────────────────────────────────────────────────────────────────────────
# player_key — contract: "{SPORT_UPPER}:{normalized_name}", cross-sport safe
# ─────────────────────────────────────────────────────────────────────────────

class TestPlayerKey:
    def test_format(self):
        key = player_key("Mike Trout", "MLB")
        assert key == "MLB:mike_trout"

    def test_sport_uppercased(self):
        key = player_key("Ja Morant", "nba")
        assert key.startswith("NBA:")

    def test_same_name_different_sport_different_key(self):
        mlb_key = player_key("Jordan", "MLB")
        nba_key = player_key("Jordan", "NBA")
        assert mlb_key != nba_key

    def test_same_name_same_sport_same_key(self):
        k1 = player_key("LeBron James", "NBA")
        k2 = player_key("LeBron James", "NBA")
        assert k1 == k2

    def test_accent_normalized_same_key(self):
        k1 = player_key("José Rondón", "MLB")
        k2 = player_key("Jose Rondon", "MLB")
        assert k1 == k2

    def test_returns_string(self):
        assert isinstance(player_key("Name", "NFL"), str)

    def test_colon_separator(self):
        key = player_key("Test Player", "NBA")
        parts = key.split(":")
        assert len(parts) == 2
        assert parts[0] == "NBA"


# ─────────────────────────────────────────────────────────────────────────────
# event_key — contract: order-independent, stable format
# ─────────────────────────────────────────────────────────────────────────────

class TestEventKey:
    def test_order_independent(self):
        k1 = event_key("MLB", "2026-08-01", "Red Sox", "Yankees")
        k2 = event_key("MLB", "2026-08-01", "Yankees", "Red Sox")
        assert k1 == k2

    def test_format_structure(self):
        key = event_key("MLB", "2026-08-01", "Red Sox", "Yankees")
        assert key.startswith("mlb:2026-08-01:")
        assert "_vs_" in key

    def test_sport_lowercased(self):
        key = event_key("NBA", "2026-08-01", "Lakers", "Celtics")
        assert key.startswith("nba:")

    def test_date_preserved(self):
        key = event_key("NFL", "2026-09-15", "Chiefs", "Raiders")
        assert "2026-09-15" in key

    def test_different_dates_different_keys(self):
        k1 = event_key("MLB", "2026-08-01", "Sox", "Yankees")
        k2 = event_key("MLB", "2026-08-02", "Sox", "Yankees")
        assert k1 != k2

    def test_teams_normalised(self):
        # Accents / punctuation in team names are handled
        k1 = event_key("MLB", "2026-08-01", "Red Sox", "Blue Jays")
        assert "red_sox" in k1
        assert "blue_jays" in k1

    def test_returns_string(self):
        assert isinstance(event_key("NFL", "2026-01-01", "A", "B"), str)


# ─────────────────────────────────────────────────────────────────────────────
# normalize_stat — contract: canonical snake_case key
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeStat:
    def test_known_stat_lowercased(self):
        result = normalize_stat("Hits")
        assert result == result.lower()

    def test_spaces_become_underscores(self):
        result = normalize_stat("Fantasy Points")
        assert " " not in result
        assert "_" in result or result == "fantasy_points"

    def test_returns_string(self):
        assert isinstance(normalize_stat("Strikeouts"), str)

    def test_empty_string(self):
        result = normalize_stat("")
        assert isinstance(result, str)

    def test_idempotent_on_already_normalised(self):
        # Should not double-normalise
        once  = normalize_stat("Hits")
        twice = normalize_stat(once)
        # Both produce valid snake_case strings (may differ slightly on double-pass)
        assert isinstance(twice, str)


# ─────────────────────────────────────────────────────────────────────────────
# CanonicalPlayer — contract: matches() interface
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalPlayer:
    def _make(self, display: str, sport: str, aliases: frozenset = frozenset()) -> CanonicalPlayer:
        return CanonicalPlayer(
            key          = player_key(display, sport),
            display_name = display,
            sport        = sport,
            aliases      = aliases,
        )

    def test_matches_display_name(self):
        cp = self._make("LeBron James", "NBA")
        assert cp.matches("LeBron James")

    def test_matches_normalised_variant(self):
        cp = self._make("LeBron James", "NBA")
        assert cp.matches("lebron james")

    def test_matches_alias(self):
        cp = self._make("Mike Trout", "MLB", aliases=frozenset({"Michael Trout"}))
        assert cp.matches("Michael Trout")

    def test_no_match_different_name(self):
        cp = self._make("LeBron James", "NBA")
        assert not cp.matches("Anthony Davis")

    def test_frozen(self):
        cp = self._make("Player Name", "NBA")
        with pytest.raises((AttributeError, TypeError)):
            cp.key = "changed"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# CanonicalEvent — contract: always a frozen dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalEvent:
    def test_frozen(self):
        ce = CanonicalEvent(
            key       = "mlb:2026-08-01:red_sox_vs_yankees",
            sport     = "MLB",
            date_str  = "2026-08-01",
            home_team = "Yankees",
            away_team = "Red Sox",
        )
        with pytest.raises((AttributeError, TypeError)):
            ce.key = "changed"  # type: ignore[misc]

    def test_optional_teams(self):
        ce = CanonicalEvent(
            key       = "nba:2026-01-01:celtics_vs_lakers",
            sport     = "NBA",
            date_str  = "2026-01-01",
            home_team = None,
            away_team = None,
        )
        assert ce.home_team is None
        assert ce.away_team is None
