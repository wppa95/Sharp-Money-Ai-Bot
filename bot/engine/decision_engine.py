"""
decision_engine.py — PrizePicks per-pick decision layer.

Sits on top of PPAnalysisScore (which is unchanged) and converts a stored
PPEdgeRecord into an actionable PPDecision: whether to bet, how much, and
what to watch for.

Public API
──────────
    decision = make_pp_decision(record)   → PPDecision
    perf     = compute_tier_performance(resolved_records) → dict[str, TierStats]

No scoring logic lives here — record.tier and record.confidence are consumed
as-is.  Kelly is computed from the stored fair probabilities and sportsbook
odds using the existing EVCalculator (engine/analysis.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import PPEdgeRecord

# Re-use the existing Kelly implementation — do not re-implement the math.
from engine.analysis import EVCalculator


# ── Action vocabulary ─────────────────────────────────────────────────────────

class PPAction:
    BET   = "BET"
    WATCH = "WATCH"
    PASS  = "PASS"


_ACTION_EMOJI: dict[str, str] = {
    PPAction.BET:   "🟢",
    PPAction.WATCH: "🟡",
    PPAction.PASS:  "⚪",
}

# ── Thresholds ────────────────────────────────────────────────────────────────

_MIN_KELLY_FOR_BET    = 0.005   # 0.5% full-Kelly floor to label a pick BET
_THIN_EDGE_PCT        = 5.0     # % — flag below this
_BIG_LINE_DIFF_UNITS  = 2.5     # units — flag large PP-vs-SB line gaps
_MIN_FAIR_PROB        = 0.52    # flag if fair probability barely clears 50%

# Quarter Kelly is standard for high-variance prop bets.
_KELLY_DIVISOR = 4.0
_MAX_UNITS     = 3.0    # hard cap per pick on a 100-unit bankroll
_MIN_UNITS_BET = 0.25   # floor when action == BET
_UNIT_STEP     = 0.25   # round to nearest quarter-unit


# ── Output dataclasses ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PPDecision:
    """Actionable decision for a single PPEdgeRecord.

    All fields are display-ready.  No scoring is embedded here.
    """
    action:          str
    kelly_full:      float   # 0–1 fraction
    kelly_half:      float   # kelly_full / 2
    kelly_quarter:   float   # kelly_full / 4  (recommended for props)
    suggested_units: float   # quarter-Kelly × 100, rounded and capped
    risk_flags:      list[str] = field(default_factory=list)

    @property
    def action_emoji(self) -> str:
        return _ACTION_EMOJI.get(self.action, "⚪")

    @property
    def action_label(self) -> str:
        return f"{self.action_emoji} {self.action}"


@dataclass
class TierStats:
    """Resolved-pick performance for a single tier."""
    tier:     str
    picks:    int
    wins:     int
    losses:   int
    pushes:   int
    avg_edge: float

    @property
    def hit_rate(self) -> float:
        contested = self.wins + self.losses
        return self.wins / contested if contested > 0 else 0.0

    @property
    def hit_rate_pct(self) -> float:
        return self.hit_rate * 100

    @property
    def sample_size_note(self) -> str:
        """Human note about sample reliability."""
        n = self.picks
        if n < 5:
            return "⚠️ tiny sample"
        if n < 15:
            return "small sample"
        return ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _round_units(raw: float) -> float:
    """Round to nearest _UNIT_STEP and clamp to [_MIN_UNITS_BET, _MAX_UNITS]."""
    rounded = round(raw / _UNIT_STEP) * _UNIT_STEP
    return max(_MIN_UNITS_BET, min(rounded, _MAX_UNITS))


def _build_risk_flags(record: "PPEdgeRecord") -> list[str]:
    flags: list[str] = []

    if (record.best_edge or 0) < _THIN_EDGE_PCT:
        flags.append("THIN EDGE")

    pp_line = record.pp_line_value or 0.0
    sb_line = record.sb_line_value or 0.0
    if pp_line and sb_line and abs(pp_line - sb_line) > _BIG_LINE_DIFF_UNITS:
        flags.append("BIG LINE DIFF")

    fair_p = (
        (record.fair_prob_over  or 0.5) if record.best_side == "OVER"
        else (record.fair_prob_under or 0.5)
    )
    if fair_p < _MIN_FAIR_PROB:
        flags.append("LOW FAIR PROB")

    return flags


# ── Public API ────────────────────────────────────────────────────────────────

def make_pp_decision(record: "PPEdgeRecord") -> PPDecision:
    """
    Convert a stored PPEdgeRecord into a PPDecision.

    Uses the stored fair probabilities and sportsbook odds to compute Kelly
    via the existing EVCalculator; derives action and unit sizing from the
    stored tier and Kelly value.

    Scoring is NOT re-run — record.tier and record.confidence are used as-is.
    """
    # Pick the correct probability and odds for the best side.
    if record.best_side == "OVER":
        fair_p       = record.fair_prob_over  or 0.5
        offered_odds = record.sb_over_odds   or -110
    else:
        fair_p       = record.fair_prob_under or 0.5
        offered_odds = record.sb_under_odds  or -110

    # Kelly via the existing EVCalculator — no new math.
    kelly_full    = EVCalculator.kelly_fraction(fair_p, offered_odds)
    kelly_half    = round(kelly_full / 2, 4)
    kelly_quarter = round(kelly_full / _KELLY_DIVISOR, 4)

    # Unit sizing: quarter Kelly on a 100-unit bankroll.
    raw_units = kelly_quarter * 100.0
    units     = _round_units(raw_units)

    # Risk flags.
    risk_flags = _build_risk_flags(record)

    # Action mapping.
    tier = (record.tier or "PASS").upper()
    if tier in ("S", "A") and kelly_full >= _MIN_KELLY_FOR_BET and "LOW FAIR PROB" not in risk_flags:
        action = PPAction.BET
    elif tier == "B" or (tier in ("S", "A") and kelly_full < _MIN_KELLY_FOR_BET):
        action = PPAction.WATCH
        units  = min(units, 0.50)
    else:
        action = PPAction.PASS
        units  = 0.0

    return PPDecision(
        action          = action,
        kelly_full      = kelly_full,
        kelly_half      = kelly_half,
        kelly_quarter   = kelly_quarter,
        suggested_units = units,
        risk_flags      = risk_flags,
    )


def compute_tier_performance(
    resolved_records: list["PPEdgeRecord"],
) -> dict[str, TierStats]:
    """
    Aggregate resolved PPEdgeRecords into per-tier TierStats.

    Only records with result in {WIN, LOSS, PUSH, REFUND} are counted.
    Tiers with zero resolved records are omitted from the output dict.
    """
    from collections import defaultdict
    buckets: dict[str, dict] = defaultdict(
        lambda: {"W": 0, "L": 0, "P": 0, "edges": []}
    )

    for r in resolved_records:
        t   = r.tier or "—"
        res = (r.result or "").upper()
        if res == "WIN":
            buckets[t]["W"] += 1
        elif res == "LOSS":
            buckets[t]["L"] += 1
        elif res in ("PUSH", "REFUND"):
            buckets[t]["P"] += 1
        else:
            continue
        if r.best_edge is not None:
            buckets[t]["edges"].append(r.best_edge)

    out: dict[str, TierStats] = {}
    for tier, data in buckets.items():
        total = data["W"] + data["L"] + data["P"]
        if total == 0:
            continue
        out[tier] = TierStats(
            tier     = tier,
            picks    = total,
            wins     = data["W"],
            losses   = data["L"],
            pushes   = data["P"],
            avg_edge = (
                sum(data["edges"]) / len(data["edges"])
                if data["edges"] else 0.0
            ),
        )

    return out
