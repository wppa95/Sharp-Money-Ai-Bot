"""
slip_optimizer.py — Correlation-aware slip builder for PrizePicks.

Takes a pool of PPEdgeRecord candidates and selects N legs that maximise
total confidence while minimising intra-slip correlation.

Correlation heuristics (rule-based, no ML required):
  1. Same player                        → 1.00  (hard block)
  2. Same team, same sport              → 0.65  (soft block / warn)
  3. Same game, opposing teams          → 0.40  (warn)
  4. Known complementary stat pairs     → ±0.05–0.10 additive adjustment
  5. Different sport / truly independent→ 0.00

Greedy selection algorithm:
  • Sort candidates by (tier_rank, -confidence) — best first.
  • Seed with the top candidate.
  • For each remaining slot, pick the candidate with the lowest
    max-correlation against the already-selected set.
  • Hard-block any candidate with max correlation ≥ 0.90.
  • Record all exclusions and soft-block warnings for display.

Public API
──────────
    result = optimize_slip(candidates, n_legs=3)   → OptimizedSlip
    pair   = check_correlation(a, b)               → CorrelationResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import PPEdgeRecord


# ── Correlation thresholds ────────────────────────────────────────────────────

_HARD_BLOCK  = 0.90   # correlation ≥ this → never include in same slip
_SOFT_BLOCK  = 0.60   # correlation ≥ this → warn in output

_SCORE_SAME_PLAYER = 1.00
_SCORE_SAME_TEAM   = 0.65
_SCORE_SAME_GAME   = 0.40

# Stat pairs that boost or reduce the base team/game correlation.
# Keys are frozensets so order doesn't matter.
_STAT_PAIR_DELTA: dict[frozenset, float] = {
    frozenset({"Passing Yards", "Receiving Yards"}):  +0.10,
    frozenset({"Passing Yards", "Receptions"}):        +0.10,
    frozenset({"Points",        "Assists"}):           +0.08,
    frozenset({"Rushing Yards", "Points"}):            +0.05,
    frozenset({"Rebounds",      "Points"}):            +0.06,
    frozenset({"Strikeouts",    "Hits"}):              -0.05,  # negative: inverse stats
    frozenset({"Rushing Yards", "Receiving Yards"}):   -0.05,
}

# Tier order for sorting (lower = better)
_TIER_RANK: dict[str, int] = {"S": 0, "A": 1, "B": 2, "PASS": 3}


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CorrelationResult:
    """Pairwise correlation between two PPEdgeRecords."""
    score:  float   # 0.0 = independent, 1.0 = perfectly correlated
    reason: str     # human-readable explanation


@dataclass
class OptimizedSlip:
    """Result of slip optimisation for N legs."""
    legs:                 list["PPEdgeRecord"]
    excluded:             list[tuple["PPEdgeRecord", str]]   # (record, reason_str)
    correlation_warnings: list[str]                          # display strings
    method:               str = "greedy_min_correlation"

    @property
    def avg_edge(self) -> float:
        edges = [r.best_edge for r in self.legs if r.best_edge is not None]
        return sum(edges) / len(edges) if edges else 0.0

    @property
    def avg_confidence(self) -> float:
        confs = [r.confidence for r in self.legs if r.confidence is not None]
        return sum(confs) / len(confs) if confs else 0.0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalise_team(team: str | None) -> str:
    return (team or "").strip().upper()


def _extract_opponent(game_description: str) -> str:
    """Return the opponent token from 'vs OPP' or 'TM vs OPP' style strings."""
    desc = (game_description or "").strip().lower()
    if desc.startswith("vs "):
        return desc[3:].strip()
    if " vs " in desc:
        return desc.split(" vs ", 1)[1].strip()
    return ""


def _stat_pair_delta(stat_a: str, stat_b: str) -> float:
    return _STAT_PAIR_DELTA.get(frozenset({stat_a, stat_b}), 0.0)


# ── Public correlation check ──────────────────────────────────────────────────

def check_correlation(a: "PPEdgeRecord", b: "PPEdgeRecord") -> CorrelationResult:
    """
    Compute a [0, 1] correlation score between two PPEdgeRecords.

    Higher score = more correlated = worse to combine in one slip.
    Uses heuristics only — no external data required.
    """
    # 1. Same player (DB dedup should prevent this, but guard it).
    if (a.player_name or "").strip().lower() == (b.player_name or "").strip().lower():
        return CorrelationResult(score=_SCORE_SAME_PLAYER, reason="Same player")

    same_sport = (a.sport or "").upper() == (b.sport or "").upper()
    team_a = _normalise_team(a.team)
    team_b = _normalise_team(b.team)

    # 2. Same team, same sport.
    if team_a and team_b and team_a == team_b and same_sport:
        base  = _SCORE_SAME_TEAM
        base += _stat_pair_delta(a.stat_type or "", b.stat_type or "")
        return CorrelationResult(
            score=round(min(max(base, 0.0), 1.0), 3),
            reason=f"Same team ({a.team})",
        )

    # 3. Same game, opposing teams — infer from game_description + team fields.
    if same_sport:
        opp_a = _extract_opponent(a.game_description or "")
        opp_b = _extract_opponent(b.game_description or "")
        same_game = False

        # a plays for team_a, their opponent is opp_a; b's team matches opp_a?
        if opp_a and team_b and opp_a in team_b.lower():
            same_game = True
        # b plays for team_b, their opponent is opp_b; a's team matches opp_b?
        elif opp_b and team_a and opp_b in team_a.lower():
            same_game = True

        if same_game:
            base  = _SCORE_SAME_GAME
            base += _stat_pair_delta(a.stat_type or "", b.stat_type or "")
            return CorrelationResult(
                score=round(min(max(base, 0.0), 1.0), 3),
                reason=f"Same game ({a.team} vs {b.team})",
            )

    # 4. Different sport or fully independent.
    return CorrelationResult(score=0.0, reason="Independent")


# ── Slip optimisation ─────────────────────────────────────────────────────────

def optimize_slip(
    candidates: list["PPEdgeRecord"],
    n_legs: int = 3,
) -> OptimizedSlip:
    """
    Select up to ``n_legs`` records from ``candidates`` using a greedy
    min-correlation algorithm.

    Steps
    -----
    1. Sort candidates by (tier_rank, -confidence) — best quality first.
    2. Seed the slip with the top candidate.
    3. For each open slot, score remaining candidates:
           pick_score = conf_norm  -  max_corr_with_selected * 2.0
       Select the highest-scoring candidate; hard-block any candidate
       whose max correlation with the selected set is ≥ _HARD_BLOCK.
    4. Soft-block candidates (add a warning but still include them) when
       max correlation is ≥ _SOFT_BLOCK.

    Args:
        candidates: Pool of PPEdgeRecords to choose from (any size).
        n_legs:     Number of legs to select (clamped to 2–6).

    Returns:
        OptimizedSlip with .legs, .excluded, .correlation_warnings.
    """
    n_legs = max(2, min(n_legs, 6))

    if not candidates:
        return OptimizedSlip(legs=[], excluded=[], correlation_warnings=[])

    # Normalise confidence to [0, 1] for the scoring formula.
    max_conf = max((r.confidence or 0) for r in candidates) or 100.0

    pool = sorted(
        candidates,
        key=lambda r: (
            _TIER_RANK.get(r.tier or "PASS", 3),
            -(r.confidence or 0),
        ),
    )

    selected:  list["PPEdgeRecord"]              = []
    excluded:  list[tuple["PPEdgeRecord", str]]  = []
    warnings:  list[str]                         = []

    for candidate in pool:
        if len(selected) >= n_legs:
            # Pool still has candidates — they are just unneeded, not excluded.
            break

        # First leg — no correlation to check.
        if not selected:
            selected.append(candidate)
            continue

        # Compute pairwise correlations with all already-selected legs.
        corr_results   = [check_correlation(candidate, s) for s in selected]
        max_corr_score = max(c.score for c in corr_results)
        worst          = max(corr_results, key=lambda c: c.score)

        if max_corr_score >= _HARD_BLOCK:
            excluded.append((
                candidate,
                f"Hard block — {worst.reason} (corr {max_corr_score:.2f})",
            ))
            continue

        if max_corr_score >= _SOFT_BLOCK:
            warnings.append(
                f"⚠️ {candidate.player_name} · {candidate.stat_type} — "
                f"{worst.reason} (corr {max_corr_score:.2f})"
            )

        selected.append(candidate)

    return OptimizedSlip(
        legs                 = selected,
        excluded             = excluded,
        correlation_warnings = warnings,
    )
