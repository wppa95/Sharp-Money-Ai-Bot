"""
engine/ud_bet_decision.py — Underdog prop betting decision layer.

Produces an OVER / UNDER / PASS recommendation for each qualified
Underdog prop based on available market signals.  This is a final
betting layer; it runs after scoring and validation and must NOT be
called for props that failed those gates.

Signal sources
──────────────
All signals are derived from line-movement history stored in our DB.
No external game-result API is integrated yet, so the following fields
are always None until result tracking is added:

  season_avg   — player's season average for this stat category
  h2h_rate     — player's hit rate in H2H matchups
  l5/l10/l20/l30 HIT rates — actual game results vs the line

What we CAN compute from market data
──────────────────────────────────────
  l5/l10/l20/l30 MOVE rates  — how often the market adjusts this prop
  avg_vs_line   — current line vs historical average (direction + magnitude)
  rate_at_or_below — line percentile in recorded history
  at_historical_low — whether this is the lowest line ever seen
  recent_move   — direction of the most recent single line change
  score.consistency — directional purity of past moves (via ud_scoring)
  score.historical_activity — overall market engagement level

Decision mapping
────────────────
  OVER   current line significantly below historical average AND market
         confirms through consistent downward pressure
  UNDER  current line significantly above historical average AND market
         confirms through consistent upward pressure
  PASS   insufficient data, conflicting signals, or edge too small

PASS is the default; the engine does not force a pick.

Public API
──────────
  UDBetDecision  — frozen dataclass
  make_ud_bet_decision(score, validation, current_line, prev_line) → UDBetDecision
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from engine.ud_scoring import UDPropScore
    from engine.player_validator import PlayerPropValidation

# ── Constants ────────────────────────────────────────────────────────────────

_MIN_EDGE_PTS     = 25    # minimum net edge required to make a directional pick
_MAX_RAW_PTS      = 120   # approximate maximum raw signal points (for confidence scaling)
_MAX_CONFIDENCE   = 95    # hard cap — never 100% without game result data
_MIN_CONFIDENCE   = 10    # floor for any computed confidence


# ── Dataclass ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UDBetDecision:
    """Betting recommendation for a single Underdog prop."""

    recommendation:  str            # "OVER" | "UNDER" | "PASS"
    confidence:      int            # 0–95 (never 100 without game-result data)
    reason:          str            # human-readable explanation

    # Market-proxy evidence (all from line-movement history)
    l5_rate:         Optional[float]    # move rate last 5 snapshots
    l10_rate:        Optional[float]    # move rate last 10 snapshots
    l20_rate:        Optional[float]    # move rate last 20 snapshots
    l30_rate:        Optional[float]    # move rate last 30 snapshots
    avg_line:        Optional[float]    # historical mean line value
    avg_vs_line_pct: Optional[float]    # (avg - current) / avg; positive = OVER signal
    rate_at_or_below:Optional[float]    # fraction of history at or below current line
    at_historical_low: bool             # current line == min ever seen

    # Reserved — always None until game-result tracking is added
    season_avg:      Optional[float]    # N/A
    h2h_rate:        Optional[float]    # N/A

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_json(self) -> str:
        """Compact JSON for storage in underdog_snapshots.bet_evidence_json."""
        def _r(v: Optional[float]) -> Optional[float]:
            return round(v, 3) if v is not None else None

        return json.dumps(
            {
                "rec":   self.recommendation,
                "conf":  self.confidence,
                "l5":    _r(self.l5_rate),
                "l10":   _r(self.l10_rate),
                "l20":   _r(self.l20_rate),
                "l30":   _r(self.l30_rate),
                "avg":   _r(self.avg_line),
                "dev":   _r(self.avg_vs_line_pct),
                "pctb":  _r(self.rate_at_or_below),
                "atlow": self.at_historical_low,
                "sea":   self.season_avg,
                "h2h":   self.h2h_rate,
            },
            separators=(",", ":"),
        )

    # ── Display helpers ──────────────────────────────────────────────────────

    def recommendation_emoji(self) -> str:
        return {"OVER": "🟢", "UNDER": "🔴", "PASS": "⚪"}.get(self.recommendation, "❓")

    def confidence_display(self) -> str:
        if self.recommendation == "PASS":
            return "—"
        return f"{self.confidence}/100"

    def avg_vs_line_display(self) -> str:
        if self.avg_vs_line_pct is None:
            return "N/A"
        pct = self.avg_vs_line_pct
        sign = "+" if pct >= 0 else ""
        avg_str = f" (avg {self.avg_line})" if self.avg_line is not None else ""
        low_str = "  ⬇️ historical low" if self.at_historical_low else ""
        return f"{sign}{pct:.1%}{avg_str}{low_str}"

    def rate_display(self, rate: Optional[float]) -> str:
        return f"{rate:.0%}" if rate is not None else "N/A"


# ── Public entry point ────────────────────────────────────────────────────────

def make_ud_bet_decision(
    score:        "UDPropScore",
    validation:   "PlayerPropValidation",
    current_line: float,
    prev_line:    Optional[float] = None,
) -> UDBetDecision:
    """
    Evaluate OVER / UNDER / PASS for a qualified Underdog prop.

    Call only when validation.has_supporting_data=True and score is not None.
    Returns PASS immediately for weak inputs.

    Parameters
    ----------
    score:        UDPropScore from score_ud_prop().
    validation:   PlayerPropValidation from validate_player_prop().
    current_line: Current prop line value.
    prev_line:    Previous line (if known) — used to extract recent move direction.
    """
    # ── Gate: insufficient data → PASS immediately ────────────────────────────
    if not validation.has_supporting_data:
        return _pass_decision(
            validation, "Insufficient history — no directional evidence available"
        )

    if score is None or score.tier == "PASS":
        return _pass_decision(
            validation, "Market signal too weak to support a directional bet"
        )

    # ── Derived signals ───────────────────────────────────────────────────────
    avg_line = validation.avg_line
    avg_vs_line_pct: Optional[float] = None

    if avg_line is not None and avg_line > 0:
        # Positive → current line is BELOW average → OVER signal
        avg_vs_line_pct = (avg_line - current_line) / avg_line

    pct_below      = validation.rate_at_or_below  # fraction ≤ current line
    at_low         = (
        validation.min_line_seen is not None
        and current_line <= validation.min_line_seen
    )
    moving_down    = prev_line is not None and current_line < prev_line
    moving_up      = prev_line is not None and current_line > prev_line

    # ── Signal scoring ────────────────────────────────────────────────────────
    over_pts  = 0
    under_pts = 0

    # 1. Avg-vs-line (primary direction signal; up to 40 pts)
    if avg_vs_line_pct is not None:
        dev = avg_vs_line_pct
        if   dev >= 0.25: over_pts  += 40
        elif dev >= 0.15: over_pts  += 30
        elif dev >= 0.08: over_pts  += 20
        elif dev >= 0.03: over_pts  += 10
        elif dev <= -0.25: under_pts += 40
        elif dev <= -0.15: under_pts += 30
        elif dev <= -0.08: under_pts += 20
        elif dev <= -0.03: under_pts += 10

    # 2. Line percentile: unusually low = OVER, unusually high = UNDER (up to 25 pts)
    if pct_below is not None:
        if   pct_below <= 0.10: over_pts  += 25
        elif pct_below <= 0.20: over_pts  += 18
        elif pct_below <= 0.30: over_pts  += 10
        elif pct_below >= 0.90: under_pts += 25
        elif pct_below >= 0.80: under_pts += 18
        elif pct_below >= 0.70: under_pts += 10

    # 3. At historical low (strong OVER signal; up to 20 pts)
    if at_low:
        over_pts += 20

    # 4. Recent move direction (up to 10 pts)
    if moving_down:
        over_pts  += 10   # market moved line down = smart-money OVER pressure
    elif moving_up:
        under_pts += 10   # market moved line up = UNDER pressure

    # 5. Market consistency amplifier — rewards high directional purity (up to 15 pts)
    consistency_factor = score.consistency / 15.0  # 0.0–1.0
    if over_pts > under_pts:
        over_pts  += int(consistency_factor * 15)
    elif under_pts > over_pts:
        under_pts += int(consistency_factor * 15)

    # 6. Market activity amplifier — rewards high move frequency (up to 10 pts)
    activity_factor = score.historical_activity / 25.0  # 0.0–1.0
    if over_pts > under_pts:
        over_pts  += int(activity_factor * 10)
    elif under_pts > over_pts:
        under_pts += int(activity_factor * 10)

    # ── Decision ──────────────────────────────────────────────────────────────
    net_edge = over_pts - under_pts

    if net_edge >= _MIN_EDGE_PTS:
        recommendation = "OVER"
        raw_pts        = over_pts
    elif net_edge <= -_MIN_EDGE_PTS:
        recommendation = "UNDER"
        raw_pts        = under_pts
    else:
        recommendation = "PASS"
        raw_pts        = max(over_pts, under_pts)

    # Scale to 0-95 confidence
    confidence = _scale_confidence(raw_pts, recommendation)

    # ── Reason string ─────────────────────────────────────────────────────────
    reason = _build_reason(
        recommendation, avg_vs_line_pct, avg_line, at_low, pct_below,
        moving_down, moving_up, net_edge,
    )

    return UDBetDecision(
        recommendation   = recommendation,
        confidence       = confidence,
        reason           = reason,
        l5_rate          = validation.l5_rate,
        l10_rate         = validation.l10_rate,
        l20_rate         = validation.l20_rate,
        l30_rate         = validation.l30_rate,
        avg_line         = avg_line,
        avg_vs_line_pct  = avg_vs_line_pct,
        rate_at_or_below = pct_below,
        at_historical_low= at_low,
        season_avg       = None,   # reserved
        h2h_rate         = None,   # reserved
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pass_decision(validation: "PlayerPropValidation", reason: str) -> UDBetDecision:
    return UDBetDecision(
        recommendation   = "PASS",
        confidence       = 0,
        reason           = reason,
        l5_rate          = getattr(validation, "l5_rate",          None),
        l10_rate         = getattr(validation, "l10_rate",         None),
        l20_rate         = getattr(validation, "l20_rate",         None),
        l30_rate         = getattr(validation, "l30_rate",         None),
        avg_line         = getattr(validation, "avg_line",         None),
        avg_vs_line_pct  = None,
        rate_at_or_below = getattr(validation, "rate_at_or_below", None),
        at_historical_low= False,
        season_avg       = None,
        h2h_rate         = None,
    )


def _scale_confidence(raw_pts: int, recommendation: str) -> int:
    """Map raw signal points to a 0-95 confidence integer."""
    if recommendation == "PASS":
        # PASS confidence is capped low; callers show "—" anyway
        return min(35, max(_MIN_CONFIDENCE, raw_pts // 3))
    scaled = int(raw_pts * _MAX_CONFIDENCE / _MAX_RAW_PTS)
    return min(_MAX_CONFIDENCE, max(_MIN_CONFIDENCE, scaled))


def _build_reason(
    recommendation: str,
    avg_dev: Optional[float],
    avg_line: Optional[float],
    at_low: bool,
    pct_below: Optional[float],
    moving_down: bool,
    moving_up: bool,
    net_edge: int,
) -> str:
    parts: list[str] = []

    if recommendation == "PASS":
        if net_edge == 0 or (avg_dev is not None and abs(avg_dev) < 0.03):
            parts.append("Signals are neutral — line is near historical average")
        else:
            parts.append("Conflicting signals — no clear directional edge")
        return "  •  ".join(parts) if parts else "Insufficient evidence for a directional pick"

    if avg_dev is not None and abs(avg_dev) >= 0.03:
        direction = "below" if avg_dev > 0 else "above"
        avg_str   = f" ({avg_line:.2f})" if avg_line is not None else ""
        parts.append(
            f"Line is {abs(avg_dev):.0%} {direction} historical average{avg_str}"
        )

    if at_low:
        parts.append("Line is at historical low — market at its most favorable for OVER")

    if pct_below is not None:
        if pct_below <= 0.30:
            parts.append(
                f"Line sits in the bottom {pct_below:.0%} of recorded history"
            )
        elif pct_below >= 0.70:
            parts.append(
                f"Line sits in the top {1 - pct_below:.0%} of recorded history"
            )

    if moving_down:
        parts.append("Market just moved line down — sharp-money OVER pressure")
    elif moving_up:
        parts.append("Market just moved line up — sharp-money UNDER pressure")

    if not parts:
        parts.append(
            "Combined market signals favour "
            + ("OVER" if recommendation == "OVER" else "UNDER")
        )

    return "  •  ".join(parts)
