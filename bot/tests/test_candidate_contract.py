"""
Contract tests — Unified Candidate Contract (Framework v3.0 Layer 2).

These tests verify that the Candidate and ConfidenceDimensions interfaces
enforce their stated invariants and that all factory adapters produce
valid, fully-populated Candidates without modifying any existing object.
"""

import pytest
from datetime import datetime
from engine.candidate import (
    Candidate,
    ConfidenceDimensions,
    VALID_DECISIONS,
    VALID_TIERS,
    VALID_RISK,
    candidate_from_ud_decision,
    candidate_from_alert_object,
    candidate_from_ev_opportunity,
    _tier_from_overall,
)
from types import SimpleNamespace


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dims(**kw) -> ConfidenceDimensions:
    defaults = dict(data_confidence=60, market_confidence=55, betting_edge=70, overall=65)
    defaults.update(kw)
    return ConfidenceDimensions(**defaults)


def _minimal_candidate(**kw) -> Candidate:
    """Build a minimal valid Candidate, overriding any field via kw."""
    defaults = dict(
        player_name = "Test Player",
        player_key  = "MLB:test_player",
        sport       = "MLB",
        stat_type   = "Hits",
        stat_key    = "hits",
        line        = 1.5,
        provider    = "Underdog",
        confidence  = _dims(),
        decision    = "OVER",
        tier        = "B",
        risk_level  = "MEDIUM",
    )
    defaults.update(kw)
    return Candidate(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# ConfidenceDimensions contract
# ─────────────────────────────────────────────────────────────────────────────

class TestConfidenceDimensions:
    def test_valid_construction(self):
        d = _dims()
        assert 0 <= d.data_confidence   <= 100
        assert 0 <= d.market_confidence <= 100
        assert 0 <= d.betting_edge      <= 100
        assert 0 <= d.overall           <= 100

    def test_boundary_zeros(self):
        d = ConfidenceDimensions(0, 0, 0, 0)
        assert d.overall == 0

    def test_boundary_hundreds(self):
        d = ConfidenceDimensions(100, 100, 100, 100)
        assert d.overall == 100

    @pytest.mark.parametrize("field,bad", [
        ("data_confidence",   -1),
        ("market_confidence", 101),
        ("betting_edge",      -10),
        ("overall",           200),
    ])
    def test_rejects_out_of_range(self, field, bad):
        kwargs = dict(data_confidence=50, market_confidence=50, betting_edge=50, overall=50)
        kwargs[field] = bad
        with pytest.raises(ValueError):
            ConfidenceDimensions(**kwargs)

    def test_frozen(self):
        d = _dims()
        with pytest.raises((AttributeError, TypeError)):
            d.overall = 99  # type: ignore[misc]

    def test_to_dict_has_all_keys(self):
        d = _dims()
        result = d.to_dict()
        assert set(result.keys()) == {
            "data_confidence", "market_confidence", "betting_edge", "overall"
        }

    def test_to_dict_values_match(self):
        d = ConfidenceDimensions(10, 20, 30, 40)
        r = d.to_dict()
        assert r["data_confidence"]   == 10
        assert r["market_confidence"] == 20
        assert r["betting_edge"]      == 30
        assert r["overall"]           == 40


# ─────────────────────────────────────────────────────────────────────────────
# Candidate — validation contract
# ─────────────────────────────────────────────────────────────────────────────

class TestCandidateValidation:
    def test_valid_construction(self):
        c = _minimal_candidate()
        assert c.player_name == "Test Player"

    @pytest.mark.parametrize("bad_decision", ["BUY", "YES", "MAYBE", "", "over"])
    def test_rejects_invalid_decision(self, bad_decision):
        with pytest.raises(ValueError, match="decision"):
            _minimal_candidate(decision=bad_decision)

    @pytest.mark.parametrize("bad_tier", ["AAA", "F", "Pass", ""])
    def test_rejects_invalid_tier(self, bad_tier):
        with pytest.raises(ValueError, match="tier"):
            _minimal_candidate(tier=bad_tier)

    @pytest.mark.parametrize("bad_risk", ["NONE", "EXTREME", "low", ""])
    def test_rejects_invalid_risk(self, bad_risk):
        with pytest.raises(ValueError, match="risk_level"):
            _minimal_candidate(risk_level=bad_risk)

    @pytest.mark.parametrize("decision", sorted(VALID_DECISIONS))
    def test_accepts_all_valid_decisions(self, decision):
        c = _minimal_candidate(decision=decision)
        assert c.decision == decision

    @pytest.mark.parametrize("tier", sorted(VALID_TIERS))
    def test_accepts_all_valid_tiers(self, tier):
        c = _minimal_candidate(tier=tier, decision="BLOCK" if tier == "BLOCK" else "OVER")
        assert c.tier == tier

    @pytest.mark.parametrize("risk", sorted(VALID_RISK))
    def test_accepts_all_valid_risk_levels(self, risk):
        c = _minimal_candidate(risk_level=risk)
        assert c.risk_level == risk


# ─────────────────────────────────────────────────────────────────────────────
# Candidate — properties and display helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestCandidateProperties:
    def test_overall_confidence_from_dims(self):
        c = _minimal_candidate(confidence=_dims(overall=77))
        assert c.overall_confidence == 77

    def test_overall_confidence_none_dims(self):
        c = _minimal_candidate(confidence=None)
        assert c.overall_confidence == 0

    def test_is_actionable_over(self):
        assert _minimal_candidate(decision="OVER").is_actionable is True

    def test_is_actionable_under(self):
        assert _minimal_candidate(decision="UNDER").is_actionable is True

    def test_is_not_actionable_pass(self):
        assert _minimal_candidate(decision="PASS").is_actionable is False

    def test_is_not_actionable_block(self):
        assert _minimal_candidate(decision="BLOCK", tier="BLOCK").is_actionable is False

    def test_to_dict_has_required_keys(self):
        c = _minimal_candidate()
        d = c.to_dict()
        for key in ("player_name", "player_key", "sport", "stat_type", "stat_key",
                    "line", "provider", "confidence", "tier", "risk_level",
                    "decision", "decision_reason", "decision_trace", "created_at"):
            assert key in d, f"Missing key: {key}"

    def test_to_json_roundtrip(self):
        import json
        c = _minimal_candidate()
        j = c.to_json()
        data = json.loads(j)
        assert data["player_name"] == "Test Player"
        assert data["decision"] == "OVER"

    def test_created_at_is_datetime(self):
        c = _minimal_candidate()
        assert isinstance(c.created_at, datetime)


# ─────────────────────────────────────────────────────────────────────────────
# Factory: candidate_from_ud_decision
# ─────────────────────────────────────────────────────────────────────────────

def _ud_decision(recommendation="OVER", tier="B", confidence=72, reason="L10: 70%"):
    """Build a minimal UDBetDecision-like namespace."""
    return SimpleNamespace(
        recommendation  = recommendation,
        decision_tier   = tier,
        confidence      = confidence,
        reason          = reason,
        l5_hit_rate     = 0.80,
        l5_games        = 5,
        l10_hit_rate    = 0.70,
        l10_games       = 10,
        l20_hit_rate    = None,
        l20_games       = None,
        l30_hit_rate    = None,
        l30_games       = None,
        season_hit_rate = None,
        season_games    = None,
    )


class TestCandidateFromUdDecision:
    def test_produces_candidate(self):
        dec = _ud_decision()
        c = candidate_from_ud_decision("Mike Trout", "MLB", "Hits", 1.5, dec)
        assert isinstance(c, Candidate)

    def test_decision_over(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision("OVER"))
        assert c.decision == "OVER"

    def test_decision_under(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision("UNDER", tier="A"))
        assert c.decision == "UNDER"

    def test_decision_pass(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision("PASS", "PASS", 0))
        assert c.decision == "PASS"

    def test_sport_uppercased(self):
        c = candidate_from_ud_decision("P", "mlb", "Hits", 1.5, _ud_decision())
        assert c.sport == "MLB"

    def test_confidence_dims_in_range(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision("OVER", "A", 85))
        assert 0 <= c.confidence.data_confidence   <= 100
        assert 0 <= c.confidence.market_confidence <= 100
        assert 0 <= c.confidence.betting_edge      <= 100
        assert 0 <= c.confidence.overall           <= 100

    def test_tier_from_decision(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision(tier="S"))
        assert c.tier == "S"

    def test_player_key_includes_sport(self):
        c = candidate_from_ud_decision("Ja Morant", "NBA", "Points", 22.5, _ud_decision())
        assert c.player_key.startswith("NBA:")
        assert "ja_morant" in c.player_key

    def test_stat_key_normalised(self):
        c = candidate_from_ud_decision("P", "NBA", "Fantasy Points", 40.5, _ud_decision())
        assert " " not in c.stat_key

    def test_provider_is_underdog(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision())
        assert c.provider == "Underdog"

    def test_window_trace_populated(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision())
        # At least l5 and l10 windows should be in the trace
        assert "l5" in c.decision_trace or "l10" in c.decision_trace

    def test_snapshot_id_optional(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision(), snapshot_id=42)
        assert c.raw_snapshot_id == 42

    def test_snapshot_id_default_none(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision())
        assert c.raw_snapshot_id is None

    def test_source_object_type(self):
        c = candidate_from_ud_decision("P", "MLB", "Hits", 1.5, _ud_decision())
        assert c.source_object_type == "UDBetDecision"

    def test_passes_validation(self):
        # If __post_init__ validation passes, the candidate is contract-valid
        c = candidate_from_ud_decision("Player Name", "NBA", "Points", 22.5, _ud_decision())
        assert c.decision in VALID_DECISIONS
        assert c.tier     in VALID_TIERS
        assert c.risk_level in VALID_RISK


# ─────────────────────────────────────────────────────────────────────────────
# Factory: candidate_from_ev_opportunity
# ─────────────────────────────────────────────────────────────────────────────

def _ev_opp(**kw):
    defaults = dict(
        sport="MLB", player="Shohei Ohtani", market_type="Moneyline",
        line=0.0, steam_score=65, expected_value=3.5, ai_confidence=72,
        best_book="Pinnacle",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestCandidateFromEvOpportunity:
    def test_produces_candidate(self):
        c = candidate_from_ev_opportunity(_ev_opp())
        assert isinstance(c, Candidate)

    def test_confidence_dims_in_range(self):
        c = candidate_from_ev_opportunity(_ev_opp())
        assert 0 <= c.confidence.data_confidence   <= 100
        assert 0 <= c.confidence.market_confidence <= 100
        assert 0 <= c.confidence.betting_edge      <= 100
        assert 0 <= c.confidence.overall           <= 100

    def test_decision_over(self):
        c = candidate_from_ev_opportunity(_ev_opp())
        assert c.decision == "OVER"

    def test_sport_uppercased(self):
        c = candidate_from_ev_opportunity(_ev_opp(sport="nba"))
        assert c.sport == "NBA"

    def test_provider_from_best_book(self):
        c = candidate_from_ev_opportunity(_ev_opp(best_book="DraftKings"))
        assert c.provider == "DraftKings"

    def test_trace_has_ev_pct(self):
        c = candidate_from_ev_opportunity(_ev_opp(expected_value=3.5))
        assert "ev_pct" in c.decision_trace

    def test_trace_has_steam_score(self):
        c = candidate_from_ev_opportunity(_ev_opp(steam_score=70))
        assert "steam_score" in c.decision_trace

    def test_negative_ev_still_valid(self):
        c = candidate_from_ev_opportunity(_ev_opp(expected_value=-2.0, ai_confidence=30))
        assert isinstance(c, Candidate)
        assert 0 <= c.confidence.betting_edge <= 100

    def test_passes_validation(self):
        c = candidate_from_ev_opportunity(_ev_opp())
        assert c.decision in VALID_DECISIONS
        assert c.tier     in VALID_TIERS


# ─────────────────────────────────────────────────────────────────────────────
# Factory: candidate_from_alert_object
# ─────────────────────────────────────────────────────────────────────────────

def _alert_obj(**kw):
    defaults = dict(
        source="Underdog", sport="NBA", market="Points",
        selection="Ja Morant", confidence=68, tier="B", reason="",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestCandidateFromAlertObject:
    def test_produces_candidate(self):
        c = candidate_from_alert_object(_alert_obj())
        assert isinstance(c, Candidate)

    def test_decision_is_pass(self):
        # AlertObject does not carry directional information
        c = candidate_from_alert_object(_alert_obj())
        assert c.decision == "PASS"

    def test_sport_uppercased(self):
        c = candidate_from_alert_object(_alert_obj(sport="nba"))
        assert c.sport == "NBA"

    def test_confidence_in_range(self):
        c = candidate_from_alert_object(_alert_obj(confidence=75))
        assert 0 <= c.confidence.overall <= 100

    def test_tier_b_default(self):
        c = candidate_from_alert_object(_alert_obj(tier="B"))
        assert c.tier == "B"

    def test_passes_validation(self):
        c = candidate_from_alert_object(_alert_obj())
        assert c.decision in VALID_DECISIONS
        assert c.tier     in VALID_TIERS
        assert c.risk_level in VALID_RISK

    def test_source_object_type(self):
        c = candidate_from_alert_object(_alert_obj())
        assert c.source_object_type == "AlertObject"


# ─────────────────────────────────────────────────────────────────────────────
# _tier_from_overall — internal helper contract
# ─────────────────────────────────────────────────────────────────────────────

class TestTierFromOverall:
    @pytest.mark.parametrize("score,expected", [
        (90, "S"), (95, "S"), (100, "S"),
        (70, "A"), (80, "A"), (89, "A"),
        (50, "B"), (60, "B"), (69, "B"),
        (0,  "PASS"), (30, "PASS"), (49, "PASS"),
    ])
    def test_tier_thresholds(self, score, expected):
        assert _tier_from_overall(score) == expected
