"""
engine/pp_scoring.py — PPAnalysisScore: five-dimension scoring for PrizePicks edges.

Dimensions
──────────
  Market Edge   0–25   How large and well-supported the edge is vs sportsbook fair odds.
  Hit Rate      0–25   Historical WIN rate for this player/stat (neutral when no history).
  Matchup       0–20   Line agreement, game-time proximity, and fair-prob decisiveness.
  Role          0–15   Stat importance and market depth/balance.
  Variance      0–15   Stat predictability, vig tightness, and PP-line stability.

  Total         0–100  Sum of all five dimensions.

Tier mapping (total → tier)
──────────────────────────
  >= 80  →  S
  >= 65  →  A
  >= 50  →  B
  <  50  →  PASS

Stars (total → stars)
──────────────────────
  >= 85  →  5★
  >= 70  →  4★
  >= 55  →  3★
  >= 40  →  2★
  <  40  →  1★

Public API
──────────
  PPAnalysisScore      — frozen dataclass with .total / .tier / .stars properties
  PPScoreTier          — "S" | "A" | "B" | "PASS" enum
  score_pp_edge(...)   — produce a PPAnalysisScore from a PPEdgeOpportunity
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from prizepicks import PPEdgeOpportunity
    from database import PPEdgeRecord


# ── Tier enum ─────────────────────────────────────────────────────────────────

class PPScoreTier(str, enum.Enum):
    S    = "S"
    A    = "A"
    B    = "B"
    PASS = "PASS"


# ── Thresholds ────────────────────────────────────────────────────────────────

_S_THRESHOLD    = 80
_A_THRESHOLD    = 65
_B_THRESHOLD    = 50

_STAR_BANDS = ((85, 5), (70, 4), (55, 3), (40, 2))   # (min_score, stars)

_HIT_RATE_NEUTRAL = 12   # score returned when there is no resolved history

# Small-sample blend weights toward neutral (n resolved records → weight on raw)
_SAMPLE_BLEND = {1: 0.40, 2: 0.55, 3: 0.70, 4: 0.85}

# Stat categories for Role scoring
_PRIMARY_STATS = frozenset({
    "Points", "Passing Yards", "Rushing Yards", "Receiving Yards",
    "Strikeouts", "Bases", "Hits",
})
_SECONDARY_STATS = frozenset({
    "Rebounds", "Assists", "Receptions", "Goals", "Shots on Goal",
})
# Specialty = anything else: "Threes Made", "Steals", "Blocks", "Touchdowns", …


# ── Score dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PPAnalysisScore:
    """Five-dimension score for a PrizePicks edge opportunity.

    All five component fields are plain integers.  ``total``, ``tier``, and
    ``stars`` are computed properties so they always stay consistent.
    """

    market_edge: int   # 0–25
    hit_rate:    int   # 0–25
    matchup:     int   # 0–20
    role:        int   # 0–15
    variance:    int   # 0–15

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return self.market_edge + self.hit_rate + self.matchup + self.role + self.variance

    @property
    def tier(self) -> str:
        t = self.total
        if t >= _S_THRESHOLD: return PPScoreTier.S.value
        if t >= _A_THRESHOLD: return PPScoreTier.A.value
        if t >= _B_THRESHOLD: return PPScoreTier.B.value
        return PPScoreTier.PASS.value

    @property
    def stars(self) -> int:
        t = self.total
        for min_score, n_stars in _STAR_BANDS:
            if t >= min_score:
                return n_stars
        return 1

    def __repr__(self) -> str:
        return (
            f"PPAnalysisScore(total={self.total}, tier={self.tier}, "
            f"stars={self.stars}\u2605 | "
            f"market_edge={self.market_edge} hit_rate={self.hit_rate} "
            f"matchup={self.matchup} role={self.role} variance={self.variance})"
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _implied(american_odds: int) -> float:
    """Raw implied probability from American odds (vig included)."""
    if american_odds < 0:
        return abs(american_odds) / (abs(american_odds) + 100.0)
    return 100.0 / (american_odds + 100.0)


def _vig_pct(over_odds: int, under_odds: int) -> float:
    """Vig as a percentage of the total implied probability."""
    return (_implied(over_odds) + _implied(under_odds) - 1.0) * 100.0


def _market_balance(over_odds: int, under_odds: int) -> float:
    """Absolute deviation of the vig-free over probability from 0.50 (0–50)."""
    total = _implied(over_odds) + _implied(under_odds)
    if total <= 0:
        return 25.0
    fair_over = _implied(over_odds) / total
    return abs(fair_over - 0.50) * 100.0


# ── Dimension 1: Market Edge (0–25) ──────────────────────────────────────────

def _score_market_edge(opp: "PPEdgeOpportunity") -> int:
    """
    Market Edge (0–25)

    Base points from ``best_edge`` (the vig-adjusted EV% advantage):
      >= 15%  → 20
      >= 12%  → 17
      >= 9%   → 14
      >= 7%   → 11
      >= 5%   →  8
      >= 3%   →  5
      else    →  0

    Bonus — adjusted fair probability (confirms the edge is real, not just line noise):
      adj_fp >= 0.62  → +3
      adj_fp >= 0.57  → +2
      adj_fp >= 0.53  → +1

    Bonus — line-diff magnitude (books disagree on the number itself):
      |line_diff| >= 2.0  → +2
      |line_diff| >= 1.0  → +1
    """
    pts = 0

    # Base
    e = opp.best_edge
    if   e >= 15: pts += 20
    elif e >= 12: pts += 17
    elif e >=  9: pts += 14
    elif e >=  7: pts += 11
    elif e >=  5: pts +=  8
    elif e >=  3: pts +=  5

    # Adjusted fair probability of the best side
    adj_fp = (
        opp.adjusted_fair_prob_over
        if opp.best_side == "OVER"
        else opp.adjusted_fair_prob_under
    )
    if   adj_fp >= 0.62: pts += 3
    elif adj_fp >= 0.57: pts += 2
    elif adj_fp >= 0.53: pts += 1

    # Line diff bonus — meaningful split between PP and sportsbook lines
    diff = abs(opp.line_diff)
    if   diff >= 2.0: pts += 2
    elif diff >= 1.0: pts += 1

    return min(pts, 25)


# ── Dimension 2: Hit Rate (0–25) ─────────────────────────────────────────────

def _score_hit_rate(history: list["PPEdgeRecord"]) -> int:
    """
    Hit Rate (0–25)

    Resolved records are WIN / LOSS / PUSH.  PENDING records are ignored.

    No resolved history          → neutral score (12)
    n < 5 resolved               → raw score blended toward neutral (small-sample discount)

    Win rate (wins / total resolved, PUSH counts as 0.5 win):
      >= 0.70  → 25
      >= 0.60  → 20
      >= 0.55  → 15
      >= 0.50  → 12
      >= 0.45  →  8
      else     →  4
    """
    resolved = [r for r in history if r.result and r.result.upper() != "PENDING"]
    n = len(resolved)
    if n == 0:
        return _HIT_RATE_NEUTRAL

    wins   = sum(1.0   for r in resolved if r.result.upper() == "WIN")
    pushes = sum(0.5   for r in resolved if r.result.upper() == "PUSH")
    win_rate = (wins + pushes) / n

    if   win_rate >= 0.70: raw = 25
    elif win_rate >= 0.60: raw = 20
    elif win_rate >= 0.55: raw = 15
    elif win_rate >= 0.50: raw = 12
    elif win_rate >= 0.45: raw =  8
    else:                  raw =  4

    # Blend toward neutral for small samples
    blend = _SAMPLE_BLEND.get(n, 1.0)
    blended = int(blend * raw + (1.0 - blend) * _HIT_RATE_NEUTRAL)
    return max(0, min(blended, 25))


# ── Dimension 3: Matchup (0–20) ───────────────────────────────────────────────

def _score_matchup(
    opp: "PPEdgeOpportunity",
    *,
    now: Optional[datetime] = None,
) -> int:
    """
    Matchup (0–20)

    Line agreement (0–8) — both books setting a similar number = confident read:
      |line_diff| == 0    → 8
      |line_diff| <= 0.5  → 7
      |line_diff| <= 1.0  → 5
      |line_diff| <= 2.0  → 3
      else                → 0

    Game-time proximity (0–8) — sharper / more info closer to game time:
      start_time is None  → 0
      <= 4 h              → 8
      <= 12 h             → 6
      <= 24 h             → 4
      <= 48 h             → 2
      else                → 0

    Fair-probability decisiveness (0–4) — how strong the adjusted edge is:
      adj_fp >= 0.60  → 4
      adj_fp >= 0.55  → 2
      else            → 0
    """
    pts = 0

    # Line agreement
    diff = abs(opp.line_diff)
    if   diff == 0.0: pts += 8
    elif diff <= 0.5: pts += 7
    elif diff <= 1.0: pts += 5
    elif diff <= 2.0: pts += 3

    # Game-time proximity
    start_time = opp.pp_line.start_time
    if start_time is not None:
        _now = now or datetime.utcnow()
        # Ensure both are naive UTC for comparison
        _start = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
        hours_to_game = (_start - _now).total_seconds() / 3600.0
        if   hours_to_game <= 4:  pts += 8
        elif hours_to_game <= 12: pts += 6
        elif hours_to_game <= 24: pts += 4
        elif hours_to_game <= 48: pts += 2

    # Adjusted fair-probability decisiveness
    adj_fp = (
        opp.adjusted_fair_prob_over
        if opp.best_side == "OVER"
        else opp.adjusted_fair_prob_under
    )
    if   adj_fp >= 0.60: pts += 4
    elif adj_fp >= 0.55: pts += 2

    return min(pts, 20)


# ── Dimension 4: Role (0–15) ──────────────────────────────────────────────────

def _score_role(opp: "PPEdgeOpportunity") -> int:
    """
    Role (0–15)

    Stat importance — primary stats track the game's most-traded markets:
      Primary   (Points, Passing Yards, Rushing Yards, Receiving Yards,
                 Strikeouts, Bases, Hits)             → 10
      Secondary (Rebounds, Assists, Receptions,
                 Goals, Shots on Goal)                →  6
      Specialty (Threes Made, Steals, Blocks,
                 Touchdowns, others)                  →  3

    Market balance (0–5) — how close each side is to fair/balanced (-110/-110):
      |fair_over − 0.50| < 1%   → 5  (deep, balanced market)
      |fair_over − 0.50| < 3%   → 4
      |fair_over − 0.50| < 5%   → 3
      |fair_over − 0.50| < 8%   → 1
      else                       → 0
    """
    pts = 0

    stat = opp.pp_line.stat_type
    if   stat in _PRIMARY_STATS:   pts += 10
    elif stat in _SECONDARY_STATS: pts +=  6
    else:                          pts +=  3

    balance = _market_balance(opp.sportsbook_over_odds, opp.sportsbook_under_odds)
    if   balance < 1.0: pts += 5
    elif balance < 3.0: pts += 4
    elif balance < 5.0: pts += 3
    elif balance < 8.0: pts += 1

    return min(pts, 15)


# ── Dimension 5: Variance (0–15) ──────────────────────────────────────────────

def _score_variance(
    opp: "PPEdgeOpportunity",
    *,
    opening_line: Optional[float] = None,
) -> int:
    """
    Variance (0–15)

    Stat stability (0–8) — low prob_per_unit means the stat is harder to move
    and less sensitive to luck.  Rewards highly predictable stat types:
      ppu <= 1.0  → 8  (e.g. Passing Yards: very stable)
      ppu <= 3.0  → 6  (e.g. Points)
      ppu <= 6.0  → 4  (e.g. Rebounds, Assists)
      ppu <= 9.0  → 2
      else        → 0

    Vig quality (0–4) — lower vig = sharper, more reliable reference pricing:
      vig <= 3%   → 4
      vig <= 5%   → 3
      vig <= 8%   → 2
      vig <= 12%  → 1
      else        → 0

    Line stability (0–3) — PP line hasn't moved much from its opening value.
    Requires opening_line to be passed in from DB history:
      |pp_line − opening_line| <= 0.5  → 3  (stable)
      |pp_line − opening_line| <= 1.5  → 1
      else or no opening_line           → 0
    """
    pts = 0

    # Stat stability
    ppu = opp.prob_per_unit
    if   ppu <= 1.0: pts += 8
    elif ppu <= 3.0: pts += 6
    elif ppu <= 6.0: pts += 4
    elif ppu <= 9.0: pts += 2

    # Vig quality
    vig = _vig_pct(opp.sportsbook_over_odds, opp.sportsbook_under_odds)
    if   vig <= 3.0:  pts += 4
    elif vig <= 5.0:  pts += 3
    elif vig <= 8.0:  pts += 2
    elif vig <= 12.0: pts += 1

    # Line stability
    if opening_line is not None:
        move = abs(opp.pp_line.line_value - opening_line)
        if   move <= 0.5: pts += 3
        elif move <= 1.5: pts += 1

    return min(pts, 15)


# ── Public entry point ────────────────────────────────────────────────────────

def score_pp_edge(
    opp: "PPEdgeOpportunity",
    *,
    history: Optional[list["PPEdgeRecord"]] = None,
    opening_line: Optional[float] = None,
    now: Optional[datetime] = None,
) -> PPAnalysisScore:
    """
    Produce a PPAnalysisScore from a PPEdgeOpportunity.

    Args:
        opp:          The computed edge opportunity (Layer 2 data).
        history:      Resolved PPEdgeRecords for the same player/stat.
                      Pass ``None`` or ``[]`` when no history is available —
                      Hit Rate will default to the neutral score (12/25).
        opening_line: First PP line ever recorded for this player/stat.
                      Used by the Variance dimension to reward line stability.
                      Pass ``None`` when this is the first detection.
        now:          Reference time for game-proximity scoring in Matchup.
                      Defaults to ``datetime.utcnow()`` when omitted.

    Returns:
        A frozen ``PPAnalysisScore`` instance.
    """
    return PPAnalysisScore(
        market_edge = _score_market_edge(opp),
        hit_rate    = _score_hit_rate(history or []),
        matchup     = _score_matchup(opp, now=now),
        role        = _score_role(opp),
        variance    = _score_variance(opp, opening_line=opening_line),
    )
