"""
engine/ud_scoring.py — UDPropScore: five-dimension scoring for Underdog Fantasy props.

Underdog is a pick'em platform (no American odds), so the scoring model is built
entirely from line-movement signals and the historical record accumulated in the
underdog_snapshots table.  Each dimension maps directly to one of the requested
grading inputs:

Dimensions
──────────
  Move Velocity       0–25   Magnitude of the current line change (sharp-action proxy).
  Historical Activity 0–25   L5/L10/L20 line-move rates ("hit rate" for pick'em props).
  Avg vs Line         0–20   Deviation of current line from its historical mean.
  Consistency         0–15   Directional purity of recorded moves (up vs down).
  Stability           0–15   Low variance of line values (predictable stat category).

  Total               0–100  Sum of all five dimensions.

Tier mapping  (total → tier)
────────────────────────────
  >= 80  →  S
  >= 65  →  A
  >= 50  →  B
  <  50  →  PASS

Stars  (total → stars)
───────────────────────
  >= 85  →  5★
  >= 70  →  4★
  >= 55  →  3★
  >= 40  →  2★
  <  40  →  1★

Alert gate (applied in underdog_job, not here)
──────────────────────────────────────────────
  Tier >= B   (stars >= 3, total >= 50)
  Removal alerts always bypass the gate.

Public API
──────────
  UDScoreTier         — "S" | "A" | "B" | "PASS" enum
  UDPropScore         — frozen dataclass with .total / .tier / .stars properties
  score_ud_prop(...)  — produce a UDPropScore from raw inputs + DB history
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import UnderdogSnapshotRecord


# ── Tier enum ─────────────────────────────────────────────────────────────────

class UDScoreTier(str, enum.Enum):
    S    = "S"
    A    = "A"
    B    = "B"
    PASS = "PASS"


# ── Thresholds ────────────────────────────────────────────────────────────────

_S_THRESHOLD = 80
_A_THRESHOLD = 65
_B_THRESHOLD = 50

_STAR_BANDS = ((85, 5), (70, 4), (55, 3), (40, 2))   # (min_score, stars)

# Neutral scores returned when there is insufficient history
_ACTIVITY_NEUTRAL    = 12   # out of 25 — same rationale as pp_scoring._HIT_RATE_NEUTRAL
_CONSISTENCY_NEUTRAL =  8   # out of 15
_STABILITY_NEUTRAL   =  8   # out of 15

# Small-sample blend weights toward neutral  n records → weight applied to raw score
_SAMPLE_BLEND = {1: 0.40, 2: 0.55, 3: 0.70, 4: 0.85}  # n >= 5 → weight = 1.0


# ── Score dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UDPropScore:
    """Five-dimension score for an Underdog Fantasy prop.

    Component fields are plain integers.  ``total``, ``tier``, ``stars``, and
    ``stars_display`` are computed properties that stay consistent with them.
    """

    player_name:         str
    stat_type:           str
    sport:               str
    current_line:        float

    move_velocity:       int   # 0–25  — magnitude of this line change
    historical_activity: int   # 0–25  — L5/L10/L20 blended move-rate
    avg_vs_line:         int   # 0–20  — deviation from historical mean
    consistency:         int   # 0–15  — directional purity of past moves
    stability:           int   # 0–15  — inverse of line-value variance

    n_history:           int   # number of DB records used for scoring

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return (
            self.move_velocity
            + self.historical_activity
            + self.avg_vs_line
            + self.consistency
            + self.stability
        )

    @property
    def tier(self) -> str:
        t = self.total
        if t >= _S_THRESHOLD: return UDScoreTier.S.value
        if t >= _A_THRESHOLD: return UDScoreTier.A.value
        if t >= _B_THRESHOLD: return UDScoreTier.B.value
        return UDScoreTier.PASS.value

    @property
    def stars(self) -> int:
        t = self.total
        for min_score, n in _STAR_BANDS:
            if t >= min_score:
                return n
        return 1

    @property
    def stars_display(self) -> str:
        n = self.stars
        return "★" * n + "☆" * (5 - n)

    def __repr__(self) -> str:
        return (
            f"UDPropScore(total={self.total}, tier={self.tier}, "
            f"stars={self.stars}★ | "
            f"vel={self.move_velocity} act={self.historical_activity} "
            f"avg={self.avg_vs_line} con={self.consistency} sta={self.stability} "
            f"n={self.n_history})"
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _score_move_velocity(magnitude: float) -> int:
    """
    Move Velocity (0–25)

    Rewards large line changes as a proxy for sharp-money conviction.
    Underdog lines move in increments of 0.5 in the majority of sports,
    so a 1.0 move is already notable; 2.0+ is rare and high-signal.

      >= 4.0  → 25
      >= 3.0  → 20
      >= 2.0  → 15
      >= 1.5  → 11
      >= 1.0  →  7
      >= 0.5  →  4
      < 0.5   →  0
    """
    if   magnitude >= 4.0: return 25
    elif magnitude >= 3.0: return 20
    elif magnitude >= 2.0: return 15
    elif magnitude >= 1.5: return 11
    elif magnitude >= 1.0: return  7
    elif magnitude >= 0.5: return  4
    else:                  return  0


def _score_historical_activity(
    history: "list[UnderdogSnapshotRecord]",
) -> int:
    """
    Historical Activity (0–25)  —  pick'em "hit rate" equivalent.

    Uses the proportion of DB records where line_moved=True across L5, L10,
    and L20 windows.  More active props (lines that adjust frequently) signal
    sharper market attention.

    Blended rate = 0.50×L5 + 0.30×L10 + 0.20×L20
      >= 0.70 → 25
      >= 0.55 → 20
      >= 0.40 → 15
      >= 0.25 → 10
      >= 0.10 →  5
      < 0.10  →  2

    No history (n < 3)  → neutral score (12).
    Small sample n < 5  → raw score blended toward neutral.
    """
    n = len(history)
    if n < 3:
        return _ACTIVITY_NEUTRAL

    def _rate(records: "list[UnderdogSnapshotRecord]") -> float:
        if not records:
            return 0.0
        return sum(1 for r in records if r.line_moved) / len(records)

    blended = (
        0.50 * _rate(history[:5])
        + 0.30 * _rate(history[:10])
        + 0.20 * _rate(history[:20])
    )

    if   blended >= 0.70: raw = 25
    elif blended >= 0.55: raw = 20
    elif blended >= 0.40: raw = 15
    elif blended >= 0.25: raw = 10
    elif blended >= 0.10: raw =  5
    else:                 raw =  2

    blend_w = _SAMPLE_BLEND.get(n, 1.0)
    return max(0, min(int(blend_w * raw + (1.0 - blend_w) * _ACTIVITY_NEUTRAL), 25))


def _score_avg_vs_line(
    current_line: float,
    history: "list[UnderdogSnapshotRecord]",
) -> int:
    """
    Avg vs Line (0–20)

    Measures how far the current line sits from its historical mean.
    A line that has migrated well away from where it started is notable
    regardless of direction — it signals sustained market adjustment.

    Requires at least 2 historical records.

      pct_dev = |current - avg| / avg × 100

      >= 25% → 20
      >= 15% → 16
      >= 10% → 12
      >=  5% →  8
      >=  2% →  4
      <  2%  →  0
    """
    values = [r.line_value for r in history[:20] if r.line_value is not None and r.line_value > 0]
    if len(values) < 2:
        return 0

    avg = sum(values) / len(values)
    if avg <= 0:
        return 0

    pct_dev = abs(current_line - avg) / avg * 100

    if   pct_dev >= 25: return 20
    elif pct_dev >= 15: return 16
    elif pct_dev >= 10: return 12
    elif pct_dev >=  5: return  8
    elif pct_dev >=  2: return  4
    else:               return  0


def _score_consistency(
    history: "list[UnderdogSnapshotRecord]",
) -> int:
    """
    Consistency (0–15)

    Directional purity: what fraction of recorded moves went the same way?
    Random oscillation (0.50 purity) suggests noise; sustained movement in
    one direction suggests genuine line pressure.

    Uses prev_line on each record to determine direction.
    Records without prev_line (first-seen entries) are ignored.

    n_moved < 2  → neutral score (8).

    purity = max(n_up, n_down) / n_moved

      >= 0.90 → 15
      >= 0.75 → 12
      >= 0.65 →  9
      >= 0.50 →  6
      < 0.50  →  3
    """
    moves = [
        (r.line_value, r.prev_line)
        for r in history
        if r.line_moved and r.prev_line is not None
    ]
    n_moved = len(moves)
    if n_moved < 2:
        return _CONSISTENCY_NEUTRAL

    n_up   = sum(1 for curr, prev in moves if curr > prev)
    n_down = n_moved - n_up
    purity = max(n_up, n_down) / n_moved

    if   purity >= 0.90: return 15
    elif purity >= 0.75: return 12
    elif purity >= 0.65: return  9
    elif purity >= 0.50: return  6
    else:                return  3


def _score_stability(
    history: "list[UnderdogSnapshotRecord]",
) -> int:
    """
    Stability (0–15)

    Inverse of line-value variance.  A stat category whose line rarely changes
    is more predictable and therefore easier to read when it does move.
    High-variance lines (e.g. speculative props) are penalised.

    n < 3  → neutral score (8).

    std_dev of line_value over last 20 records:
      <= 0.25 → 15
      <= 0.50 → 12
      <= 0.75 →  9
      <= 1.25 →  5
      <= 2.00 →  2
      > 2.00  →  0
    """
    values = [
        r.line_value
        for r in history[:20]
        if r.line_value is not None and not r.removed
    ]
    if len(values) < 3:
        return _STABILITY_NEUTRAL

    try:
        std = statistics.stdev(values)
    except statistics.StatisticsError:
        return _STABILITY_NEUTRAL

    if   std <= 0.25: return 15
    elif std <= 0.50: return 12
    elif std <= 0.75: return  9
    elif std <= 1.25: return  5
    elif std <= 2.00: return  2
    else:             return  0


# ── Public entry point ────────────────────────────────────────────────────────

def score_ud_prop(
    player_name:  str,
    stat_type:    str,
    sport:        str,
    current_line: float,
    prev_line:    Optional[float],
    history:      "list[UnderdogSnapshotRecord]",
) -> UDPropScore:
    """
    Produce a UDPropScore for an Underdog prop.

    Parameters
    ----------
    player_name:   Display name of the player.
    stat_type:     Stat category (e.g. "Fantasy Points", "Hits").
    sport:         Sport code, upper-cased (e.g. "MLB", "NFL").
    current_line:  The line value just fetched from the API.
    prev_line:     The line value from the previous DB record, or None if
                   this is the first detection.  Used for Move Velocity.
    history:       Most-recent-first list of UnderdogSnapshotRecord rows for
                   this player+stat from the database.  Pass [] when no history
                   exists — Activity / Consistency / Stability return neutral
                   scores; Avg vs Line returns 0.

    Returns
    -------
    A frozen UDPropScore instance.
    """
    magnitude = abs(current_line - prev_line) if prev_line is not None else 0.0

    return UDPropScore(
        player_name         = player_name,
        stat_type           = stat_type,
        sport               = sport,
        current_line        = current_line,
        move_velocity       = _score_move_velocity(magnitude),
        historical_activity = _score_historical_activity(history),
        avg_vs_line         = _score_avg_vs_line(current_line, history),
        consistency         = _score_consistency(history),
        stability           = _score_stability(history),
        n_history           = len(history),
    )
