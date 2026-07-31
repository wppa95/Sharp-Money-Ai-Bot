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


# ── Prop difficulty classification ───────────────────────────────────────────

class PropDifficultyClass(str, enum.Enum):
    """
    Three-tier classification of prop difficulty / variance.

    HIGH_FLOOR    — reliable daily-production markets; preferred "green goblin"
                    opportunities.  No score adjustment.
    STANDARD      — typical prop categories; no adjustment.
    HIGH_VARIANCE — low-frequency or volatile outcomes (e.g. HR 0.5, SB 0.5).
                    A variance_penalty is subtracted from the raw total so that
                    a higher evidence bar must be cleared before these alerts fire.
    """
    HIGH_FLOOR    = "HIGH_FLOOR"
    STANDARD      = "STANDARD"
    HIGH_VARIANCE = "HIGH_VARIANCE"


# Stat categories that produce high-floor, daily-production outcomes.
# These are the "green goblin" targets — reliable hits for active players.
_HIGH_FLOOR_STATS: frozenset[str] = frozenset({
    "Hits",
    "Hits + Runs + RBIs",
    "Fantasy Score",
    "Fantasy Points",
    "Points",
    "Rebounds",
    "Points + Rebounds + Assists",
    "Pts+Rebs+Asts",
    "PRA",
    "Assists",
    "Pass Completions",
    "Receiving Yards",
    "Rushing Yards",
    "Passing Yards",
})

# Stat categories that are inherently volatile — low-frequency outcomes where
# small samples mislead and single-game variance is very high.
_HIGH_VARIANCE_STATS: frozenset[str] = frozenset({
    "Home Runs",
    "Stolen Bases",
    "RBIs",
    "Wins",
    "Saves",
})

# At these exact line values even otherwise-standard stats become high-variance.
# e.g. "Hits 0.5" is qualitatively different from "Hits 1.5".
_HIGH_VARIANCE_LINES: frozenset[float] = frozenset({0.5})


# ── Thresholds ────────────────────────────────────────────────────────────────

_S_THRESHOLD = 80
_A_THRESHOLD = 65
_B_THRESHOLD = 50

_STAR_BANDS = ((85, 5), (70, 4), (55, 3), (40, 2))   # (min_score, stars)

# Neutral scores returned when there is insufficient history.
# _ACTIVITY_NEUTRAL recalibrated from 12→5 for Underdog's observed move-rate
# distribution (median 0%, p99 ≈3.3%, maximum observed ≈19%).  The original
# sportsbook-derived value is kept as _ACTIVITY_NEUTRAL_LEGACY for the
# before/after comparison logged at the end of every cold-start cycle.
_ACTIVITY_NEUTRAL        =  5   # out of 25 — Underdog-calibrated neutral
_ACTIVITY_NEUTRAL_LEGACY = 12   # original sportsbook-derived value; comparison only
_CONSISTENCY_NEUTRAL     =  8   # out of 15
_STABILITY_NEUTRAL       =  8   # out of 15

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

    # ── Difficulty / variance adjustment (default = no adjustment) ────────────
    # Fields have defaults so existing call sites that omit them still work.
    variance_penalty:    int                = 0                             # 0, 5, or 10 — subtracted from raw total
    difficulty:          PropDifficultyClass = PropDifficultyClass.STANDARD # prop difficulty class

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        """Raw component sum minus variance penalty (floor 0)."""
        return max(0, (
            self.move_velocity
            + self.historical_activity
            + self.avg_vs_line
            + self.consistency
            + self.stability
            - self.variance_penalty
        ))

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

    Thresholds calibrated to Underdog's observed distribution
    (91 524 records; median per-prop rate 0%, p99 ≈3.3%, max ≈19%):

      >= 0.15 → 25   top ≈0.2% — maximum observed rate (≈19%)
      >= 0.10 → 20   very active — only ~4 props in corpus
      >= 0.05 → 15   active — p99+ (~top 1%)
      >= 0.02 → 10   notable movement
      >= 0.005 →  5  occasional movement
      < 0.005 →  2   effectively static (99.8% of props)

    No history (n < 3)  → neutral score (5).
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

    if   blended >= 0.15:  raw = 25
    elif blended >= 0.10:  raw = 20
    elif blended >= 0.05:  raw = 15
    elif blended >= 0.02:  raw = 10
    elif blended >= 0.005: raw =  5
    else:                  raw =  2

    blend_w = _SAMPLE_BLEND.get(n, 1.0)
    return max(0, min(int(blend_w * raw + (1.0 - blend_w) * _ACTIVITY_NEUTRAL), 25))


def _score_historical_activity_legacy(
    history: "list[UnderdogSnapshotRecord]",
) -> int:
    """
    Legacy Historical Activity — original sportsbook-calibrated thresholds.

    Kept solely for the before/after comparison log emitted at the end of
    each cold-start cycle.  Do NOT use for live scoring; use
    _score_historical_activity() instead.
    """
    n = len(history)
    if n < 3:
        return _ACTIVITY_NEUTRAL_LEGACY

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
    return max(0, min(int(blend_w * raw + (1.0 - blend_w) * _ACTIVITY_NEUTRAL_LEGACY), 25))


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


def _classify_prop_difficulty(stat_type: str, line_value: float) -> PropDifficultyClass:
    """
    Classify a prop's difficulty tier based on stat type and line value.

    HIGH_FLOOR  — safe, reliable daily-production markets; no score adjustment.
    HIGH_VARIANCE — low-frequency outcomes or 0.5-line bets on volatile stats;
                    a variance_penalty will be subtracted from their raw total,
                    requiring stronger evidence before they reach a qualifying tier.
    STANDARD    — everything else; no adjustment.

    Note: ``_HIGH_FLOOR_STATS`` at a 0.5 line are still classified HIGH_FLOOR
    because high-floor stats are consistently achievable even at low thresholds
    (e.g. "at least one hit" for a contact hitter is genuinely reliable).
    """
    # Explicitly volatile categories are HIGH_VARIANCE regardless of line
    if stat_type in _HIGH_VARIANCE_STATS:
        return PropDifficultyClass.HIGH_VARIANCE

    # Standard stats at a 0.5 line become HIGH_VARIANCE (not high-floor stats)
    if line_value in _HIGH_VARIANCE_LINES and stat_type not in _HIGH_FLOOR_STATS:
        return PropDifficultyClass.HIGH_VARIANCE

    if stat_type in _HIGH_FLOOR_STATS:
        return PropDifficultyClass.HIGH_FLOOR

    return PropDifficultyClass.STANDARD


def _score_variance_penalty(stat_type: str, line_value: float) -> int:
    """
    Variance penalty (0, 5, or 10) — subtracted from the raw five-dimension
    total before tier and star assignment.

    HIGH_VARIANCE props require stronger evidence to reach the same tier:
      Explicit high-variance stat + 0.5 line (HR 0.5, RBI 0.5, SB 0.5) → –10
      General high-variance (category or 0.5 non-high-floor line)       → –5
      HIGH_FLOOR or STANDARD                                              →  0
    """
    dc = _classify_prop_difficulty(stat_type, line_value)
    if dc != PropDifficultyClass.HIGH_VARIANCE:
        return 0
    # Harshest penalty: explicitly volatile stat AND a 0.5 line
    if line_value in _HIGH_VARIANCE_LINES and stat_type in _HIGH_VARIANCE_STATS:
        return 10
    return 5


def _score_drift_velocity(opening_line: float, current_line: float) -> int:
    """
    Drift Velocity (0–15) — cold-start baseline for accumulated line migration.

    Applied only when prev_line is unavailable (cold-start path) but at least
    one history record exists.  Unlike Move Velocity — which captures the
    conviction of a single sharp-money event — this captures the cumulative
    drift between the prop's earliest recorded line and the current value.

    Capped at 15 (vs 25 for live velocity) to reflect softer conviction:
    a prop that drifted 1.0 over 30 cycles is less actionable than one that
    moved 1.0 in a single session.

    Underdog lines move in 0.5 increments, so thresholds mirror that:
      drift >= 2.0 → 15   (4+ steps — sustained directional pressure)
      drift >= 1.0 → 10   (2 steps — meaningful cumulative shift)
      drift >= 0.5 →  5   (1 step — line moved at least once from opening)
      < 0.5        →  0   (line stable since first observation)
    """
    drift = abs(current_line - opening_line)
    if   drift >= 2.0: return 15
    elif drift >= 1.0: return 10
    elif drift >= 0.5: return  5
    else:              return  0


# ── Public entry point ────────────────────────────────────────────────────────

def score_ud_prop(
    player_name:        str,
    stat_type:          str,
    sport:              str,
    current_line:       float,
    prev_line:          Optional[float],
    history:            "list[UnderdogSnapshotRecord]",
    use_drift_velocity: bool = False,
) -> UDPropScore:
    """
    Produce a UDPropScore for an Underdog prop.

    Parameters
    ----------
    player_name:          Display name of the player.
    stat_type:            Stat category (e.g. "Fantasy Points", "Hits").
    sport:                Sport code, upper-cased (e.g. "MLB", "NFL").
    current_line:         The line value just fetched from the API.
    prev_line:            The line value from the previous DB record, or None
                          if this is the first detection.  Used for Move
                          Velocity.
    history:              Most-recent-first list of UnderdogSnapshotRecord rows
                          for this player+stat.  Pass [] when no history exists
                          — Activity / Consistency / Stability return neutral
                          scores; Avg vs Line and drift velocity return 0.
    use_drift_velocity:   When True and prev_line is None, estimate velocity
                          from the cumulative drift between the oldest history
                          record and the current line (cold-start path only).
                          Has no effect when prev_line is set or history is
                          empty.  Default False.

    Returns
    -------
    A frozen UDPropScore instance.
    """
    if prev_line is not None:
        # Live line-change event — standard sharp-money velocity.
        velocity = _score_move_velocity(abs(current_line - prev_line))
    elif use_drift_velocity and history:
        # Cold-start baseline: cumulative drift from the earliest known line.
        # history is most-recent-first, so history[-1] is the oldest record.
        opening = history[-1].line_value
        velocity = _score_drift_velocity(opening, current_line) if opening is not None else 0
    else:
        velocity = 0

    penalty    = _score_variance_penalty(stat_type, current_line)
    difficulty = _classify_prop_difficulty(stat_type, current_line)

    return UDPropScore(
        player_name         = player_name,
        stat_type           = stat_type,
        sport               = sport,
        current_line        = current_line,
        move_velocity       = velocity,
        historical_activity = _score_historical_activity(history),
        avg_vs_line         = _score_avg_vs_line(current_line, history),
        consistency         = _score_consistency(history),
        stability           = _score_stability(history),
        n_history           = len(history),
        variance_penalty    = penalty,
        difficulty          = difficulty,
    )
