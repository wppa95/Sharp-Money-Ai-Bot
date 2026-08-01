"""
Unified Candidate Contract — Sharp Money Bot Framework v3.0 Layer 2.

All decisions flow through one Candidate object.  Systems that currently
produce UDBetDecision, EVOpportunity, PPEdgeOpportunity, or AlertObject are
adapted via the factory functions at the bottom of this module — no existing
call sites need to change.

Contract invariants (enforced by __post_init__)
───────────────────────────────────────────────
• player_key   = engine.identity.player_key(player_name, sport)
• stat_key     = engine.identity.normalize_stat(stat_type)
• confidence   = ConfidenceDimensions with all fields in [0, 100]
• tier         ∈ {"S", "A", "B", "PASS", "BLOCK"}
• decision     ∈ {"OVER", "UNDER", "PASS", "BLOCK"}
• risk_level   ∈ {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

Backward compatibility
──────────────────────
All factory functions are ADDITIVE.  Existing UDBetDecision, EVOpportunity, and
AlertObject objects continue to work unchanged.  Candidates are produced
alongside them, not instead of them, until a future migration phase replaces
the originals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from engine.identity import normalize_stat, player_key as _player_key

if TYPE_CHECKING:
    # Only used for type annotations — never at runtime (avoids circular imports)
    from engine.ud_bet_decision import UDBetDecision
    from models import AlertObject, EVOpportunity

# ─────────────────────────────────────────────────────────────────────────────
# Valid contract values — changing these is a breaking migration
# ─────────────────────────────────────────────────────────────────────────────

VALID_DECISIONS  = frozenset({"OVER", "UNDER", "PASS", "BLOCK"})
VALID_TIERS      = frozenset({"S", "A", "B", "PASS", "BLOCK"})
VALID_RISK       = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
VALID_LEARNING   = frozenset({"Model", "Market", "Settlement", "Variance", None})


# ─────────────────────────────────────────────────────────────────────────────
# ConfidenceDimensions — 4-part confidence separation (Framework Layer 5)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConfidenceDimensions:
    """
    Four-dimension confidence separation (Framework v3.0 Layer 5).

    Defined here so the Candidate contract is self-contained.  A dedicated
    Confidence Separation implementation will populate these from the existing
    scoring engines in a later phase.

    data_confidence   — How reliable is the underlying information?
                        (sample size, data recency, source count)
    market_confidence — How reliable is the market movement signal?
                        (velocity, activity, consistency, stability)
    betting_edge      — Does actual betting value exist?
                        (hit-rate deviation, window agreement, H2H)
    overall           — Final recommendation strength.
                        (synthesised from the three dimensions above)

    All dimensions are integers in [0, 100].
    """

    data_confidence:   int   # 0–100
    market_confidence: int   # 0–100
    betting_edge:      int   # 0–100
    overall:           int   # 0–100

    def __post_init__(self) -> None:
        for dim in ("data_confidence", "market_confidence", "betting_edge", "overall"):
            v = getattr(self, dim)
            if not isinstance(v, int) or not (0 <= v <= 100):
                raise ValueError(
                    f"ConfidenceDimensions.{dim} must be an integer in [0, 100], got {v!r}"
                )

    def to_dict(self) -> dict:
        return {
            "data_confidence":   self.data_confidence,
            "market_confidence": self.market_confidence,
            "betting_edge":      self.betting_edge,
            "overall":           self.overall,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Candidate — the unified decision container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """
    Unified decision container — the single object that flows through all systems.

    Build via the factory functions at the bottom of this module:
        candidate_from_ud_decision()
        candidate_from_ev_opportunity()
        candidate_from_alert_object()
    """

    # ── Player / prop identity ─────────────────────────────────────────────────
    player_name: str
    player_key:  str        # engine.identity.player_key(player_name, sport)
    sport:       str        # uppercase sport string
    stat_type:   str        # raw stat label from source
    stat_key:    str        # engine.identity.normalize_stat(stat_type)
    line:        float
    provider:    str        # originating provider name

    # ── Event identity ─────────────────────────────────────────────────────────
    event_key:   Optional[str]      = None   # engine.identity.event_key(...)
    game_time:   Optional[datetime] = None

    # ── Confidence (4-dimension) ───────────────────────────────────────────────
    confidence:  Optional[ConfidenceDimensions] = None

    # ── Decision ───────────────────────────────────────────────────────────────
    tier:            str = "PASS"     # S / A / B / PASS / BLOCK
    risk_level:      str = "MEDIUM"   # LOW / MEDIUM / HIGH / CRITICAL
    decision:        str = "PASS"     # OVER / UNDER / PASS / BLOCK
    decision_reason: str = ""
    decision_trace:  dict = field(default_factory=dict)  # serialisable artifacts

    # ── Raw snapshot reference ─────────────────────────────────────────────────
    raw_snapshot_id:   Optional[int] = None
    raw_snapshot_type: str           = ""   # class name of originating DB record

    # ── Learning ──────────────────────────────────────────────────────────────
    # Set after result is known: "Model" | "Market" | "Settlement" | "Variance"
    learning_classification: Optional[str] = None

    # ── Metadata ──────────────────────────────────────────────────────────────
    created_at:         datetime = field(default_factory=datetime.utcnow)
    source_object_type: str      = ""   # class name of the source object

    # ─────────────────────────────────────────────────────────────────────────
    # Contract validation
    # ─────────────────────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        if self.decision not in VALID_DECISIONS:
            raise ValueError(
                f"Candidate.decision must be one of {sorted(VALID_DECISIONS)}, "
                f"got {self.decision!r}"
            )
        if self.tier not in VALID_TIERS:
            raise ValueError(
                f"Candidate.tier must be one of {sorted(VALID_TIERS)}, "
                f"got {self.tier!r}"
            )
        if self.risk_level not in VALID_RISK:
            raise ValueError(
                f"Candidate.risk_level must be one of {sorted(VALID_RISK)}, "
                f"got {self.risk_level!r}"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Display helpers
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def overall_confidence(self) -> int:
        """Convenience accessor for the overall confidence dimension."""
        return self.confidence.overall if self.confidence else 0

    @property
    def is_actionable(self) -> bool:
        """True when the decision is OVER or UNDER (not PASS or BLOCK)."""
        return self.decision in ("OVER", "UNDER")

    def to_dict(self) -> dict:
        return {
            "player_name":    self.player_name,
            "player_key":     self.player_key,
            "sport":          self.sport,
            "stat_type":      self.stat_type,
            "stat_key":       self.stat_key,
            "line":           self.line,
            "provider":       self.provider,
            "event_key":      self.event_key,
            "game_time":      self.game_time.isoformat() if self.game_time else None,
            "confidence":     self.confidence.to_dict() if self.confidence else None,
            "tier":           self.tier,
            "risk_level":     self.risk_level,
            "decision":       self.decision,
            "decision_reason":self.decision_reason,
            "decision_trace": self.decision_trace,
            "raw_snapshot_id":       self.raw_snapshot_id,
            "raw_snapshot_type":     self.raw_snapshot_type,
            "learning_classification": self.learning_classification,
            "created_at":     self.created_at.isoformat(),
            "source_object_type": self.source_object_type,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Factory adapters — produce Candidates from existing objects (non-breaking)
# ─────────────────────────────────────────────────────────────────────────────

def candidate_from_ud_decision(
    player_name: str,
    sport: str,
    stat_type: str,
    line: float,
    decision: Any,                    # UDBetDecision — Any to avoid import
    *,
    game_time: Optional[datetime] = None,
    snapshot_id: Optional[int]    = None,
) -> Candidate:
    """
    Produce a Candidate from a ``UDBetDecision`` + raw prop identity fields.

    UDBetDecision is a frozen dataclass that does not store player/sport/stat;
    the caller must supply those alongside the decision object.

    Confidence mapping (interim — Confidence Separation phase refines this)
    ─────────────────────────────────────────────────────────────────────────
    data_confidence   ← tier proxy: S→80 / A→70 / B→60 / PASS→30
    market_confidence ← 50 neutral (UDPropScore not available here)
    betting_edge      ← decision.confidence (0–95)
    overall           ← decision.confidence (primary signal for betting value)
    """
    tier = getattr(decision, "decision_tier", "PASS")
    conf = int(getattr(decision, "confidence", 0))
    rec  = getattr(decision, "recommendation", "PASS")
    reason = str(getattr(decision, "reason", ""))

    data_conf = {"S": 80, "A": 70, "B": 60, "PASS": 30}.get(tier, 50)
    dims = ConfidenceDimensions(
        data_confidence   = data_conf,
        market_confidence = 50,
        betting_edge      = conf,
        overall           = conf,
    )

    # Serialise window evidence into the decision trace
    trace: dict = {"tier": tier, "confidence": conf}
    for window in ("l5", "l10", "l20", "l30", "season"):
        hr = getattr(decision, f"{window}_hit_rate", None)
        gm = getattr(decision, f"{window}_games", None)
        if hr is not None and gm:
            trace[window] = {"games": gm, "hit_rate": round(hr, 4)}

    return Candidate(
        player_name       = player_name,
        player_key        = _player_key(player_name, sport),
        sport             = sport.upper(),
        stat_type         = stat_type,
        stat_key          = normalize_stat(stat_type),
        line              = float(line),
        provider          = "Underdog",
        game_time         = game_time,
        confidence        = dims,
        tier              = tier if tier in VALID_TIERS else "PASS",
        risk_level        = {"S": "LOW", "A": "LOW", "B": "MEDIUM", "PASS": "HIGH"}.get(tier, "MEDIUM"),
        decision          = rec if rec in VALID_DECISIONS else "PASS",
        decision_reason   = reason,
        decision_trace    = trace,
        raw_snapshot_id   = snapshot_id,
        raw_snapshot_type = "UnderdogSnapshot",
        source_object_type= "UDBetDecision",
    )


def candidate_from_ev_opportunity(opp: Any) -> Candidate:
    """
    Produce a Candidate from an ``EVOpportunity`` (EV / Steam sportsbook alerts).

    Confidence mapping
    ──────────────────
    data_confidence   ← 70 baseline (sportsbook data is generally reliable)
    market_confidence ← steam_score (0–100 already)
    betting_edge      ← EV% scaled: 0%→50, +5%→100, negative→<50
    overall           ← opp.ai_confidence (stored on EVOpportunity)
    """
    steam     = min(100, max(0, int(getattr(opp, "steam_score", 50) or 50)))
    ev_pct    = float(getattr(opp, "expected_value", 0.0) or 0.0)
    edge_conf = min(100, max(0, int(50 + ev_pct * 10)))
    overall   = int(getattr(opp, "ai_confidence", edge_conf) or edge_conf)

    sport_val  = str(getattr(opp, "sport", "") or "")
    player_val = str(getattr(opp, "player", "") or getattr(opp, "selection", "") or "unknown")
    stat_val   = str(getattr(opp, "market_type", "") or "")
    line_val   = float(getattr(opp, "line", 0.0) or 0.0)
    book_val   = str(getattr(opp, "best_book", "Unknown") or "Unknown")

    dims = ConfidenceDimensions(
        data_confidence   = 70,
        market_confidence = steam,
        betting_edge      = edge_conf,
        overall           = overall,
    )

    return Candidate(
        player_name        = player_val,
        player_key         = _player_key(player_val, sport_val),
        sport              = sport_val.upper() if sport_val else "UNKNOWN",
        stat_type          = stat_val,
        stat_key           = normalize_stat(stat_val),
        line               = line_val,
        provider           = book_val,
        confidence         = dims,
        tier               = _tier_from_overall(overall),
        risk_level         = "MEDIUM",
        decision           = "OVER",   # EV alerts are always toward the positive-EV side
        decision_reason    = f"EV: {ev_pct:+.2f}%  steam: {steam}/100",
        decision_trace     = {
            "ev_pct":         ev_pct,
            "steam_score":    steam,
            "ai_confidence":  overall,
        },
        raw_snapshot_type  = "EVOpportunity",
        source_object_type = "EVOpportunity",
    )


def candidate_from_alert_object(obj: Any) -> Candidate:
    """
    Produce a Candidate from a normalised ``AlertObject``.

    AlertObject is the generic envelope built by alert_normalizer.py.
    This adapter is the fallback for alert types without a dedicated factory.
    Confidence dimensions default to neutral (50) because AlertObject does not
    carry the raw signal breakdown required to populate all four dimensions.
    """
    conf_val   = int(getattr(obj, "confidence", 50) or 50)
    tier_raw   = str(getattr(obj, "tier", "B") or "B")
    # AlertTier can be an enum instance — extract .value if needed
    if hasattr(tier_raw, "value"):
        tier_raw = tier_raw.value
    tier_map = {
        "S": "S", "A": "A", "B": "B", "PASS": "PASS",
        "Critical": "S", "High": "A", "Medium": "B", "Low": "PASS",
    }
    tier_out   = tier_map.get(tier_raw, "B")

    sport_val  = str(getattr(obj, "sport", "UNKNOWN") or "UNKNOWN")
    sel_val    = str(getattr(obj, "selection", "") or "")
    source_val = str(getattr(obj, "source", "Unknown") or "Unknown")
    market_val = str(getattr(obj, "market", "") or "")

    dims = ConfidenceDimensions(
        data_confidence   = 50,
        market_confidence = 50,
        betting_edge      = conf_val,
        overall           = conf_val,
    )
    return Candidate(
        player_name        = sel_val,
        player_key         = _player_key(sel_val, sport_val),
        sport              = sport_val.upper(),
        stat_type          = market_val,
        stat_key           = normalize_stat(market_val),
        line               = 0.0,
        provider           = source_val,
        confidence         = dims,
        tier               = tier_out,
        risk_level         = "MEDIUM",
        decision           = "PASS",
        decision_reason    = str(getattr(obj, "reason", "") or ""),
        decision_trace     = {},
        raw_snapshot_type  = "AlertObject",
        source_object_type = "AlertObject",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tier_from_overall(overall: int) -> str:
    if overall >= 90: return "S"
    if overall >= 70: return "A"
    if overall >= 50: return "B"
    return "PASS"
