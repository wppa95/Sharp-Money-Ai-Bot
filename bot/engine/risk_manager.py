"""
engine/risk_manager.py — Risk Management Engine (Framework v3.0 Layer 10).

Identifies portfolio-level risk factors across a set of Candidate objects.

Risk factors detected
─────────────────────
  CORRELATION       — Two or more candidates are from the same game or event.
  SAME_GAME         — Multiple props from the same game_id (strongest correlation).
  PLAYER_DEPENDENCY — Two or more props from the same player in the portfolio.
  HIGH_VARIANCE     — The sport/stat combination has inherently high variance
                      (from prop_intelligence SportAdapter).
  POSITION_SIZE     — More than N correlated picks in a single game.

Design constraints
──────────────────
• Risk adjusts recommendations — it NEVER creates duplicate rankings or confidence scores.
• assess_risk() and assess_portfolio_risk() are pure functions. No IO, no async.
• Severity is always LOW | MEDIUM | HIGH.
• recommendation_adjustment drives Analyst narrative only — not tier recomputation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from engine.candidate import Candidate

# ── Risk factor codes ──────────────────────────────────────────────────────────

RISK_CODES = frozenset({
    "CORRELATION",
    "SAME_GAME",
    "PLAYER_DEPENDENCY",
    "HIGH_VARIANCE",
    "POSITION_SIZE",
})

_HIGH_VARIANCE_SPORTS = frozenset({"MLB", "NFL", "NHL"})

# Stat types that are inherently volatile (irrespective of sport)
_HIGH_VARIANCE_STATS = frozenset({
    "home_runs", "strikeouts", "touchdowns", "passing_yards",
    "rushing_yards", "aces", "double_faults",
})


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskFactor:
    """
    A single identified risk factor for a Candidate within a portfolio.

    code        : One of RISK_CODES.
    severity    : "LOW" | "MEDIUM" | "HIGH"
    description : Human-readable explanation stored in Analyst narrative.
    """
    code:        str   # one of RISK_CODES
    severity:    str   # "LOW" | "MEDIUM" | "HIGH"
    description: str


@dataclass(frozen=True)
class RiskAssessment:
    """
    Risk assessment for one Candidate within a portfolio context.

    composite_risk            : Highest severity of all detected factors.
    recommendation_adjustment : "NONE" | "REDUCE" | "AVOID"
    factors                   : All detected risk factors; may be empty.
    correlated_players        : Names of other players in the same game/event.
    """
    player_key:               str
    player_name:              str
    sport:                    str
    stat_type:                str
    composite_risk:           str   # "LOW" | "MEDIUM" | "HIGH"
    recommendation_adjustment:str   # "NONE" | "REDUCE" | "AVOID"
    factors:                  tuple  # tuple[RiskFactor, ...]
    correlated_players:       tuple  # tuple[str, ...] player names in same game


# ── Severity helpers ──────────────────────────────────────────────────────────

_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _max_severity(severities: list[str]) -> str:
    if not severities:
        return "LOW"
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0))


def _adjustment_from_severity(severity: str, factor_count: int) -> str:
    if severity == "HIGH" or factor_count >= 3:
        return "AVOID"
    if severity == "MEDIUM" or factor_count >= 2:
        return "REDUCE"
    return "NONE"


# ── Core assessment functions ─────────────────────────────────────────────────

def assess_risk(
    candidate: "Candidate",
    portfolio: Optional[list["Candidate"]] = None,
) -> RiskAssessment:
    """
    Assess portfolio-level risk for one Candidate given the current portfolio.

    Parameters
    ----------
    candidate : The Candidate being evaluated.
    portfolio : All active Candidates in the current recommendation set.
                Pass None or [] for a standalone (no-portfolio) assessment.

    Returns
    -------
    RiskAssessment — pure data; apply to Analyst narrative or Telegram output.
    """
    portfolio = [c for c in (portfolio or []) if c is not candidate]
    factors: list[RiskFactor] = []
    correlated: list[str] = []

    # ── HIGH_VARIANCE check ───────────────────────────────────────────────────
    sport = (candidate.sport or "").upper()
    stat  = (candidate.stat_type or "").lower()

    if sport in _HIGH_VARIANCE_SPORTS:
        factors.append(RiskFactor(
            code        = "HIGH_VARIANCE",
            severity    = "MEDIUM",
            description = (
                f"{sport} is a high-variance sport — individual props are "
                "more susceptible to single-game outlier results."
            ),
        ))

    if any(kw in stat for kw in _HIGH_VARIANCE_STATS):
        factors.append(RiskFactor(
            code        = "HIGH_VARIANCE",
            severity    = "MEDIUM",
            description = (
                f"'{candidate.stat_type}' is a high-variance stat — "
                "rare events inflate both hit and miss rates."
            ),
        ))

    if not portfolio:
        # Standalone assessment — no portfolio factors
        sev = _max_severity([f.severity for f in factors])
        return RiskAssessment(
            player_key                = candidate.player_key,
            player_name               = candidate.player_name,
            sport                     = candidate.sport,
            stat_type                 = candidate.stat_type,
            composite_risk            = sev,
            recommendation_adjustment = "NONE",
            factors                   = tuple(factors),
            correlated_players        = (),
        )

    # ── PLAYER_DEPENDENCY check ───────────────────────────────────────────────
    same_player = [
        c for c in portfolio
        if c.player_key == candidate.player_key
    ]
    if same_player:
        n = len(same_player)
        factors.append(RiskFactor(
            code        = "PLAYER_DEPENDENCY",
            severity    = "HIGH" if n >= 2 else "MEDIUM",
            description = (
                f"{n + 1} props from {candidate.player_name} in the portfolio — "
                "all are exposed to the same playing-time and game-result risk."
            ),
        ))

    # ── SAME_GAME / CORRELATION checks ────────────────────────────────────────
    cand_game = _extract_game_key(candidate)
    same_game_candidates = [
        c for c in portfolio if _extract_game_key(c) == cand_game and cand_game
    ]

    if same_game_candidates:
        correlated = [c.player_name for c in same_game_candidates]
        n = len(same_game_candidates)

        # Exact same game_id = SAME_GAME; approximate (same team/event prefix) = CORRELATION
        if candidate.event_key and all(
            c.event_key == candidate.event_key for c in same_game_candidates
        ):
            sev = "HIGH" if n >= 2 else "MEDIUM"
            factors.append(RiskFactor(
                code        = "SAME_GAME",
                severity    = sev,
                description = (
                    f"{n + 1} props in the same game — "
                    f"correlated with: {', '.join(correlated[:3])}."
                ),
            ))
        else:
            factors.append(RiskFactor(
                code        = "CORRELATION",
                severity    = "MEDIUM" if n >= 2 else "LOW",
                description = (
                    f"Correlated props detected in similar events: "
                    f"{', '.join(correlated[:3])}."
                ),
            ))

        # ── POSITION_SIZE check ───────────────────────────────────────────────
        if n >= 3:
            factors.append(RiskFactor(
                code        = "POSITION_SIZE",
                severity    = "HIGH",
                description = (
                    f"{n + 1} correlated picks from the same game — "
                    "over-concentration in one event."
                ),
            ))

    composite = _max_severity([f.severity for f in factors])
    adjustment = _adjustment_from_severity(composite, len(factors))

    return RiskAssessment(
        player_key                = candidate.player_key,
        player_name               = candidate.player_name,
        sport                     = candidate.sport,
        stat_type                 = candidate.stat_type,
        composite_risk            = composite,
        recommendation_adjustment = adjustment,
        factors                   = tuple(factors),
        correlated_players        = tuple(correlated),
    )


def assess_portfolio_risk(
    candidates: list["Candidate"],
) -> list[RiskAssessment]:
    """
    Assess risk for all Candidates in a portfolio simultaneously.

    Returns one RiskAssessment per Candidate in the same order as *candidates*.

    Parameters
    ----------
    candidates : The full set of active Candidates to evaluate.
    """
    return [
        assess_risk(c, [x for x in candidates if x is not c])
        for c in candidates
    ]


def portfolio_risk_summary(assessments: list[RiskAssessment]) -> str:
    """Return a plain-text summary of portfolio risk for Telegram / console output."""
    if not assessments:
        return "No active candidates — portfolio risk not applicable."

    high   = sum(1 for a in assessments if a.composite_risk == "HIGH")
    medium = sum(1 for a in assessments if a.composite_risk == "MEDIUM")
    low    = sum(1 for a in assessments if a.composite_risk == "LOW")
    avoid  = sum(1 for a in assessments if a.recommendation_adjustment == "AVOID")
    reduce = sum(1 for a in assessments if a.recommendation_adjustment == "REDUCE")

    lines = [
        f"Portfolio: {len(assessments)} candidates",
        f"  Risk:  HIGH {high}  MEDIUM {medium}  LOW {low}",
    ]
    if avoid:
        lines.append(f"  AVOID ({avoid} picks due to high correlation/position-size)")
    if reduce:
        lines.append(f"  REDUCE ({reduce} picks — correlated, reduce stake)")

    return "\n".join(lines)


# ── Private helpers ────────────────────────────────────────────────────────────

def _extract_game_key(candidate: "Candidate") -> Optional[str]:
    """
    Extract a game identity key from a Candidate for correlation detection.

    Priority: event_key → raw_snapshot_id prefix (game portion).
    Returns None when no game context is available.
    """
    if candidate.event_key:
        return candidate.event_key
    return None
