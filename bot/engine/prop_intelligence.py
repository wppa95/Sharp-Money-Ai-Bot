"""
prop_intelligence.py — Player Prop Intelligence Engine (Framework v3.0 Layer 8).

Five-layer contextual intelligence system that enriches Candidate confidence
dimensions with player-specific context.  All intelligence is computed once,
stored as structured reasoning in decision_trace, and never re-run inside the
Explanation Service.

Layer architecture
──────────────────
1. Historical Performance Intelligence
   Analyse L5/L10/L20/L30 windows from Underdog history.
   Produces: WindowStats, HistoricalIntelligence, Sample Strength Score.
   Maps to: Candidate.confidence.data_confidence (via data_confidence_delta)

2. Role & Usage Intelligence
   Infer playing-time stability and usage trend from line-value patterns.
   Produces: RoleIntelligence with a bounded signal.
   Maps to: Candidate.confidence.betting_edge (via betting_edge_delta)

3. Sport-Specific Prop Framework
   Static adapters per sport — not a new engine, just configuration.
   Defines: relevant_stats, variance_level, sample_requirements, risk_rules.
   Applied by: compute_historical_intelligence(), compute_prop_intelligence().

4. Opponent Matchup Intelligence
   Infer matchup context from recent line-movement patterns.
   Produces: MatchupIntelligence with a bounded signal.
   Maps to: Candidate.confidence.betting_edge (via betting_edge_delta)

5. Recommendation Upgrade
   Aggregate all layers into PropIntelligenceResult.
   Applied via: Candidate.with_prop_intelligence(result).

Design constraints (non-negotiable)
────────────────────────────────────
• No new scoring engine.  All new intelligence is an additive layer on top of
  the existing UDPropScore + UDBetDecision signals.
• No recalculation inside ExplanationService.  All reasoning is stored in
  intelligence_trace at compute time.
• Pure functions — no async, no DB calls.  Data must be pre-fetched by caller.
• Works with UnderdogSnapshotRecord ORM objects, dicts, or SimpleNamespaces.
"""

from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _g(obj: Any, attr: str, default: Any = None) -> Any:
    """Get attribute from ORM object, dict, or SimpleNamespace gracefully."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _std(values: list) -> float:
    """Population standard deviation; returns 0.0 for n < 2."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def _directional_consistency(deltas: list) -> float:
    """
    Fraction of non-zero deltas moving in the same direction as the majority.
    Returns 0.5 when no directional data is available.
    """
    active = [d for d in deltas if d != 0]
    if not active:
        return 0.5
    pos = sum(1 for d in active if d > 0)
    neg = len(active) - pos
    return max(pos, neg) / len(active)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Sport-Specific Prop Framework (defined first; used by other layers)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SportAdapter:
    """
    Sport-specific configuration for prop analysis.

    Not a scoring engine — purely a configuration object.  Each adapter
    defines the statistical context the intelligence layers use to calibrate
    sample requirements and variance expectations.
    """
    sport: str

    # Stats where this sport generates reliable prop markets
    relevant_stats: frozenset

    # Inherent variance level of this sport's props
    # LOW → small samples more reliable; HIGH → need larger samples
    variance_level: str  # "LOW" | "MEDIUM" | "HIGH"

    # Minimum sample for any confidence signal
    min_samples: int

    # Sample thresholds for S / A / B tier recommendations
    sample_requirements: dict  # {"S": int, "A": int, "B": int}

    # Risk rules applied during tier adjustment
    # Supported keys:
    #   "cap_tier_below_n"  : (tier_cap, n_threshold) — cap tier below this sample
    #   "high_variance_cap" : tier_cap string — cap at this tier for high-var props
    risk_rules: dict


SPORT_ADAPTERS: dict[str, SportAdapter] = {

    "NBA": SportAdapter(
        sport             = "NBA",
        relevant_stats    = frozenset({
            "points", "rebounds", "assists", "steals", "blocks",
            "threes_made", "turnovers", "minutes", "field_goals_made",
            "free_throws_made",
        }),
        variance_level    = "MEDIUM",
        min_samples       = 5,
        sample_requirements = {"S": 20, "A": 10, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),   # cap at B when n < 5
            "high_variance_cap": "A",       # volatile stats capped at A
        },
    ),

    "WNBA": SportAdapter(
        sport             = "WNBA",
        relevant_stats    = frozenset({
            "points", "rebounds", "assists", "steals", "blocks",
            "threes_made", "turnovers", "minutes",
        }),
        variance_level    = "MEDIUM",
        min_samples       = 5,
        sample_requirements = {"S": 15, "A": 8, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),
            "high_variance_cap": "A",
        },
    ),

    "MLB": SportAdapter(
        sport             = "MLB",
        relevant_stats    = frozenset({
            "hits", "strikeouts", "home_runs", "rbi", "walks", "total_bases",
            "pitching_strikeouts", "earned_runs", "innings_pitched",
            "runs_scored",
        }),
        variance_level    = "HIGH",
        min_samples       = 7,
        sample_requirements = {"S": 25, "A": 15, "B": 7},
        risk_rules        = {
            "cap_tier_below_n": ("B", 7),
            "high_variance_cap": "B",       # MLB is high-variance → cap at B
        },
    ),

    "NFL": SportAdapter(
        sport             = "NFL",
        relevant_stats    = frozenset({
            "passing_yards", "rushing_yards", "receiving_yards", "touchdowns",
            "receptions", "completions", "passing_attempts",
        }),
        variance_level    = "HIGH",
        min_samples       = 5,
        sample_requirements = {"S": 15, "A": 10, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),
            "high_variance_cap": "B",
        },
    ),

    "NHL": SportAdapter(
        sport             = "NHL",
        relevant_stats    = frozenset({
            "shots_on_goal", "points", "goals", "assists", "blocked_shots",
            "power_play_points", "saves",
        }),
        variance_level    = "HIGH",
        min_samples       = 5,
        sample_requirements = {"S": 20, "A": 10, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),
            "high_variance_cap": "B",
        },
    ),

    "TENNIS": SportAdapter(
        sport             = "TENNIS",
        relevant_stats    = frozenset({
            "aces", "double_faults", "games_won", "sets_won", "service_games",
            "first_serve_percentage",
        }),
        variance_level    = "MEDIUM",
        min_samples       = 5,
        sample_requirements = {"S": 15, "A": 8, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),
            "high_variance_cap": "A",
        },
    ),

    "CS": SportAdapter(
        sport             = "CS",
        relevant_stats    = frozenset({
            "kills", "deaths", "assists", "headshots", "adr", "rating",
            "rounds_played",
        }),
        variance_level    = "MEDIUM",
        min_samples       = 5,
        sample_requirements = {"S": 15, "A": 8, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),
            "high_variance_cap": "A",
        },
    ),

    "DEFAULT": SportAdapter(
        sport             = "DEFAULT",
        relevant_stats    = frozenset(),
        variance_level    = "MEDIUM",
        min_samples       = 5,
        sample_requirements = {"S": 20, "A": 10, "B": 5},
        risk_rules        = {
            "cap_tier_below_n": ("B", 5),
            "high_variance_cap": "A",
        },
    ),
}


def get_sport_adapter(sport: str) -> SportAdapter:
    """Return the SportAdapter for *sport*, falling back to DEFAULT."""
    return SPORT_ADAPTERS.get((sport or "").upper(), SPORT_ADAPTERS["DEFAULT"])


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Historical Performance Intelligence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowStats:
    """
    Statistics for a single historical window (L5/L10/L20/L30 or overall).

    All values are computed from Underdog line history — not from actual game
    results (which are in player_game_results and PropOpportunityLog).

    avg_vs_line : (average historical line) - current_line.
                  Positive = historical average was higher than current line
                  (potentially OVER-friendly — line may have risen artificially).
                  Negative = current line is above historical average
                  (harder to hit OVER).

    hit_rate    : Proxy OVER hit rate from validation_json rate_below field.
                  1.0 - rate_below (fraction of historical lines above current line).
                  -1.0 when unknown (no validation data available).

    consistency : Directional purity of line movements (0–1).
                  1.0 = all moves in the same direction; 0.5 = random.

    variance    : Standard deviation of line values over the window.
    """
    n:           int
    avg_vs_line: float    # (avg historical line) - current_line; positive = OVER-friendly
    hit_rate:    float    # 0.0–1.0 proxy; -1.0 = unknown
    consistency: float    # 0.0–1.0 directional purity
    variance:    float    # std dev of line values in this window


@dataclass(frozen=True)
class HistoricalIntelligence:
    """
    Historical performance intelligence for one player × stat × line.

    Window stats cover the last N snapshots in Underdog history.
    sample_strength (0–100) combines sample size and line variance.
    data_confidence_delta is the bounded adjustment to Candidate.data_confidence.
    """
    l5:    Optional[WindowStats]  # last 5  snapshots
    l10:   Optional[WindowStats]  # last 10 snapshots
    l20:   Optional[WindowStats]  # last 20 snapshots
    l30:   Optional[WindowStats]  # last 30 snapshots
    overall: WindowStats          # all available snapshots
    sample_strength:       int    # 0–100 composite quality score
    data_confidence_delta: int    # -20 to +20, applied to Candidate.data_confidence


def _sample_strength(n: int, variance: float) -> int:
    """
    Map sample size + line variance to Sample Strength Score (0–100).

    Small samples and high variance both reduce confidence.
    Large, low-variance samples signal a reliably priced prop.

    Parameters
    ----------
    n        : Number of historical snapshots available.
    variance : Standard deviation of line values (float).
    """
    if n >= 30: base = 80
    elif n >= 20: base = 70
    elif n >= 15: base = 60
    elif n >= 10: base = 50
    elif n >= 5:  base = 35
    else:         base = 10

    # Variance adjustment only meaningful when n >= 3; fewer points can't
    # establish a reliable variance estimate so the adjustment is suppressed.
    if n >= 3:
        if variance < 0.5:   var_adj = +15
        elif variance < 1.0: var_adj = +8
        elif variance < 2.0: var_adj = 0
        elif variance < 3.5: var_adj = -10
        else:                var_adj = -20
    else:
        var_adj = 0

    return _clamp(base + var_adj, 0, 100)


def _build_window_stats(
    lines:        list,
    deltas:       list,
    current_line: float,
    hit_rate:     float,
) -> WindowStats:
    n = len(lines)
    if n == 0:
        return WindowStats(n=0, avg_vs_line=0.0, hit_rate=-1.0, consistency=0.0, variance=0.0)
    avg        = sum(lines) / n
    std        = _std(lines)
    consistency = _directional_consistency(deltas)
    avg_vs_line = avg - current_line
    return WindowStats(
        n           = n,
        avg_vs_line = round(avg_vs_line, 3),
        hit_rate    = round(hit_rate, 3) if hit_rate >= 0 else -1.0,
        consistency = round(consistency, 3),
        variance    = round(std, 3),
    )


def compute_historical_intelligence(
    history: list,
    line:    float,
    adapter: Optional[SportAdapter] = None,
) -> HistoricalIntelligence:
    """
    Compute historical performance intelligence from Underdog prop history.

    Works with UnderdogSnapshotRecord ORM objects, plain dicts, or
    SimpleNamespace objects — any object with line_value/line, line_delta,
    fetched_at, and optionally validation_json attributes.

    Parameters
    ----------
    history : List of snapshot records, any order.
    line    : Current prop line being evaluated.
    adapter : Optional SportAdapter for sport-specific calibration.
    """
    if not history:
        empty = _build_window_stats([], [], line, -1.0)
        return HistoricalIntelligence(
            l5=None, l10=None, l20=None, l30=None,
            overall=empty,
            sample_strength=10,
            data_confidence_delta=-20,
        )

    # Sort oldest → newest
    def _sort_key(r: Any) -> datetime:
        ts = _g(r, 'fetched_at')
        return ts if ts is not None else datetime.min

    sorted_hist = sorted(history, key=_sort_key)

    # Extract line values (support both field names)
    all_lines: list = []
    for r in sorted_hist:
        v = _g(r, 'line_value') or _g(r, 'line')
        if v is not None:
            try:
                all_lines.append(float(v))
            except (TypeError, ValueError):
                pass

    all_deltas: list = []
    for r in sorted_hist:
        d = _g(r, 'line_delta')
        if d is not None:
            try:
                all_deltas.append(float(d))
            except (TypeError, ValueError):
                pass

    # Extract validation_json from most recent record that has it
    val_data: dict = {}
    for rec in reversed(sorted_hist):
        vj = _g(rec, 'validation_json')
        if vj:
            try:
                parsed = json.loads(vj) if isinstance(vj, str) else vj
                if isinstance(parsed, dict):
                    val_data = parsed
                    break
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    n_total = len(all_lines)
    overall_variance = _std(all_lines)
    sample_str = _sample_strength(n_total, overall_variance)

    # OVER hit rate proxy: 1 - rate_below (fraction of history ABOVE current line)
    rate_below = val_data.get('rate_below')
    overall_hr = (1.0 - float(rate_below)) if rate_below is not None else -1.0

    overall_ws = _build_window_stats(all_lines, all_deltas, line, overall_hr)

    def _ws(n: int, key: str, min_needed: int) -> Optional[WindowStats]:
        if n_total < min_needed:
            return None
        w_lines  = all_lines[-n:]
        w_deltas = all_deltas[-n:]
        rate     = val_data.get(key)
        hr       = (1.0 - float(rate)) if rate is not None else -1.0
        return _build_window_stats(w_lines, w_deltas, line, hr)

    l5_ws  = _ws(5,  'l5',  3)
    l10_ws = _ws(10, 'l10', 7)
    l20_ws = _ws(20, 'l20', 15)
    l30_ws = _ws(30, 'l30', 20)

    # data_confidence_delta
    min_s = adapter.min_samples if adapter else 5
    if sample_str >= 80 and n_total >= 20:
        delta = +20
    elif sample_str >= 65 and n_total >= 10:
        delta = +10
    elif sample_str >= 45 and n_total >= min_s:
        delta = 0
    elif n_total >= 3:
        delta = -10
    else:
        delta = -20

    return HistoricalIntelligence(
        l5              = l5_ws,
        l10             = l10_ws,
        l20             = l20_ws,
        l30             = l30_ws,
        overall         = overall_ws,
        sample_strength = sample_str,
        data_confidence_delta = _clamp(delta, -20, +20),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Role & Usage Intelligence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RoleIntelligence:
    """
    Inferred playing-time and usage context for a player prop.

    Derived from line-value patterns in Underdog history — not from
    play-by-play data (which is unavailable here).

    role_label       : "Starter" | "Bench" | "Unknown"
    minutes_stability: "Stable" | "Moderate" | "Volatile" | "Insufficient"
    usage_trend      : "Rising" | "Flat" | "Falling" | "Unknown"
    signal           : -15 to +15 adjustment for betting_edge
    summary          : Human-readable stored reasoning (Explanation Service reads this)
    """
    role_label:        str   # "Starter" | "Bench" | "Unknown"
    minutes_stability: str   # "Stable" | "Moderate" | "Volatile" | "Insufficient"
    usage_trend:       str   # "Rising" | "Flat" | "Falling" | "Unknown"
    signal:            int   # -15 to +15
    summary:           str   # stored reasoning for ExplanationService


_STARTER_KEYWORDS  = frozenset({"points", "minutes", "rebounds", "assists", "blocks",
                                 "steals", "field_goals_made", "threes_made", "kills",
                                 "passing_yards", "rushing_yards", "receiving_yards"})
_MINIMAL_HISTORY   = 3


def compute_role_intelligence(
    history:   list,
    stat_type: str,
    sport:     str,
) -> RoleIntelligence:
    """
    Infer role and usage context from line-value history patterns.

    Positive signals (Starter, Stable, Rising):   signal = up to +15
    Negative signals (Bench, Volatile, Falling):  signal = down to -15
    Insufficient data:                            signal = 0, role = Unknown

    Parameters
    ----------
    history   : List of snapshot records for this player × stat.
    stat_type : Prop stat name (e.g. "points", "minutes").
    sport     : Sport string (uppercase).
    """
    if len(history) < _MINIMAL_HISTORY:
        return RoleIntelligence(
            role_label        = "Unknown",
            minutes_stability = "Insufficient",
            usage_trend       = "Unknown",
            signal            = 0,
            summary           = "Insufficient history for role analysis.",
        )

    # Sort oldest → newest, take last 10
    def _sort_key(r: Any) -> datetime:
        ts = _g(r, 'fetched_at')
        return ts if ts is not None else datetime.min

    recent = sorted(history, key=_sort_key)[-10:]

    lines: list = []
    for r in recent:
        v = _g(r, 'line_value') or _g(r, 'line')
        if v is not None:
            try:
                lines.append(float(v))
            except (TypeError, ValueError):
                pass

    if len(lines) < _MINIMAL_HISTORY:
        return RoleIntelligence(
            role_label        = "Unknown",
            minutes_stability = "Insufficient",
            usage_trend       = "Unknown",
            signal            = 0,
            summary           = "Insufficient line data for role analysis.",
        )

    mean = sum(lines) / len(lines)
    std  = _std(lines)

    # Coefficient of variation → stability
    cv = (std / mean) if mean > 0 else 1.0
    if cv < 0.08:
        stability = "Stable"
        stab_signal = +8
    elif cv < 0.20:
        stability = "Moderate"
        stab_signal = +2
    else:
        stability = "Volatile"
        stab_signal = -10

    # Role from stability + stat type
    is_volume_stat = any(kw in stat_type.lower() for kw in _STARTER_KEYWORDS)
    if stability == "Stable" and is_volume_stat:
        role = "Starter"
    elif stability == "Volatile":
        role = "Bench"
    else:
        role = "Unknown"

    # Usage trend: recent half vs earlier half
    mid  = len(lines) // 2
    if mid >= 2:
        earlier_avg = sum(lines[:mid]) / mid
        recent_avg  = sum(lines[mid:]) / len(lines[mid:])
        if earlier_avg > 0:
            change = (recent_avg - earlier_avg) / earlier_avg
            if change > 0.05:
                trend = "Rising"
                trend_signal = +7
            elif change < -0.05:
                trend = "Falling"
                trend_signal = -7
            else:
                trend = "Flat"
                trend_signal = 0
        else:
            trend = "Unknown"
            trend_signal = 0
    else:
        trend = "Unknown"
        trend_signal = 0

    # Role bonus
    role_signal = +5 if role == "Starter" else (-5 if role == "Bench" else 0)

    signal  = _clamp(stab_signal + trend_signal + role_signal, -15, +15)

    # Build human-readable summary
    parts = []
    if role != "Unknown":
        parts.append(f"{role} role")
    parts.append(f"{stability.lower()} minutes")
    if trend != "Unknown":
        parts.append(f"{trend.lower()} usage")
    summary = ", ".join(parts).capitalize() + "." if parts else "Role context unavailable."

    return RoleIntelligence(
        role_label        = role,
        minutes_stability = stability,
        usage_trend       = trend,
        signal            = signal,
        summary           = summary,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — Opponent Matchup Intelligence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MatchupIntelligence:
    """
    Inferred opponent matchup context, derived from recent line-move patterns.

    Since direct opponent data is not available in Underdog history,
    matchup quality is inferred from net line movement before games:
    - Consecutive upward movement → market prices a favourable matchup.
    - Downward movement → market prices a tough matchup.
    - No movement → Neutral.

    matchup_label : "Favorable" | "Neutral" | "Tough" | "Unknown"
    signal        : -15 to +15 adjustment for betting_edge
    reasoning     : Stored text for ExplanationService (never recalculated)
    """
    matchup_label: str   # "Favorable" | "Neutral" | "Tough" | "Unknown"
    signal:        int   # -15 to +15
    reasoning:     str   # stored for ExplanationService


def compute_matchup_intelligence(
    history:        list,
    line:           float,
    adapter:        Optional[SportAdapter] = None,
) -> MatchupIntelligence:
    """
    Infer opponent matchup context from Underdog line-movement history.

    Uses net line_delta over the most recent 5 snapshots as a proxy for
    market-implied matchup quality.  Direct opponent statistics are not
    available from Underdog history; this is explicitly a proxy approach.

    Parameters
    ----------
    history : List of snapshot records for this player × stat.
    line    : Current prop line.
    adapter : Optional SportAdapter (currently unused; reserved for future use).
    """
    if not history:
        return MatchupIntelligence(
            matchup_label = "Unknown",
            signal        = 0,
            reasoning     = "No history available for matchup analysis.",
        )

    def _sort_key(r: Any) -> datetime:
        ts = _g(r, 'fetched_at')
        return ts if ts is not None else datetime.min

    recent = sorted(history, key=_sort_key)[-5:]

    deltas: list = []
    for r in recent:
        d = _g(r, 'line_delta')
        if d is not None:
            try:
                deltas.append(float(d))
            except (TypeError, ValueError):
                pass

    if not deltas:
        return MatchupIntelligence(
            matchup_label = "Unknown",
            signal        = 0,
            reasoning     = "No line movement data available for matchup inference.",
        )

    net_delta = sum(deltas)

    if net_delta >= 1.0:
        label     = "Favorable"
        signal    = +10
        reasoning = (
            f"Market moved line up {net_delta:.1f} units over recent snapshots — "
            f"opponent matchup signal is favorable."
        )
    elif net_delta <= -1.0:
        label     = "Tough"
        signal    = -10
        reasoning = (
            f"Market moved line down {abs(net_delta):.1f} units over recent snapshots — "
            f"opponent matchup signal is tough."
        )
    elif net_delta >= 0.5:
        label     = "Favorable"
        signal    = +5
        reasoning = (
            f"Slight upward line movement ({net_delta:.1f} units) — "
            f"mild favorable matchup signal."
        )
    elif net_delta <= -0.5:
        label     = "Tough"
        signal    = -5
        reasoning = (
            f"Slight downward line movement ({abs(net_delta):.1f} units) — "
            f"mild tough matchup signal."
        )
    else:
        label     = "Neutral"
        signal    = 0
        reasoning = (
            f"Net line movement {net_delta:+.2f} — no significant matchup signal."
        )

    return MatchupIntelligence(
        matchup_label = label,
        signal        = signal,
        reasoning     = reasoning,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 5 — Aggregation: PropIntelligenceResult + compute_prop_intelligence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PropIntelligenceResult:
    """
    Aggregated result from all prop intelligence layers.

    Apply to a Candidate via Candidate.with_prop_intelligence(result).
    The intelligence_trace dict is stored verbatim into Candidate.decision_trace
    under the key "prop_intelligence".  ExplanationService reads it from there —
    it never re-runs compute_prop_intelligence().

    Fields
    ------
    historical            : HistoricalIntelligence from Layer 1
    role                  : RoleIntelligence from Layer 2
    matchup               : MatchupIntelligence from Layer 4
    sport_adapter         : SportAdapter from Layer 3
    data_confidence_delta : Bounded (-20..+20) delta for Candidate.data_confidence
    betting_edge_delta    : Bounded (-20..+20) delta for Candidate.betting_edge
    intelligence_trace    : Serialisable dict for Candidate.decision_trace storage
    """
    historical:            HistoricalIntelligence
    role:                  RoleIntelligence
    matchup:               MatchupIntelligence
    sport_adapter:         SportAdapter
    data_confidence_delta: int   # -20 to +20
    betting_edge_delta:    int   # -20 to +20
    intelligence_trace:    dict


def compute_prop_intelligence(
    player_name: str,
    sport:       str,
    stat_type:   str,
    line:        float,
    history:     list,
) -> PropIntelligenceResult:
    """
    Compute all prop intelligence layers and return an aggregated result.

    Call Candidate.with_prop_intelligence(result) to apply the result.

    This is a pure function — no async, no DB calls.  The caller is
    responsible for pre-fetching the history list from the database.

    Parameters
    ----------
    player_name : Player name string (for trace logging only).
    sport       : Sport string (e.g. "NBA", "MLB").
    stat_type   : Prop stat type (e.g. "points", "home_runs").
    line        : Current prop line value.
    history     : List of snapshot records (UnderdogSnapshotRecord objects,
                  dicts, or SimpleNamespaces).
    """
    adapter = get_sport_adapter(sport)

    historical = compute_historical_intelligence(history, line, adapter)
    role       = compute_role_intelligence(history, stat_type, sport)
    matchup    = compute_matchup_intelligence(history, line, adapter)

    # Aggregate deltas — clamped at ±20 each
    data_delta = _clamp(historical.data_confidence_delta, -20, +20)
    bet_delta  = _clamp(role.signal + matchup.signal,     -20, +20)

    # Build serialisable intelligence trace
    trace: dict = {
        "player_name": player_name,
        "sport":       sport,
        "stat_type":   stat_type,
        "line":        line,
        "historical": {
            "sample_strength":       historical.sample_strength,
            "n":                     historical.overall.n,
            "avg_vs_line":           historical.overall.avg_vs_line,
            "hit_rate":              historical.overall.hit_rate,
            "consistency":           historical.overall.consistency,
            "variance":              historical.overall.variance,
            "data_confidence_delta": data_delta,
            "windows": {
                "l5":  {"n": historical.l5.n,  "hit_rate": historical.l5.hit_rate}  if historical.l5  else None,
                "l10": {"n": historical.l10.n, "hit_rate": historical.l10.hit_rate} if historical.l10 else None,
                "l20": {"n": historical.l20.n, "hit_rate": historical.l20.hit_rate} if historical.l20 else None,
                "l30": {"n": historical.l30.n, "hit_rate": historical.l30.hit_rate} if historical.l30 else None,
            },
        },
        "role": {
            "label":     role.role_label,
            "stability": role.minutes_stability,
            "trend":     role.usage_trend,
            "signal":    role.signal,
            "summary":   role.summary,
        },
        "matchup": {
            "label":     matchup.matchup_label,
            "signal":    matchup.signal,
            "reasoning": matchup.reasoning,
        },
        "sport_adapter": {
            "sport":          adapter.sport,
            "variance_level": adapter.variance_level,
            "min_samples":    adapter.min_samples,
        },
        "adjustments": {
            "data_confidence_delta": data_delta,
            "betting_edge_delta":    bet_delta,
        },
    }

    logger.debug(
        "prop_intelligence: %s / %s / %s  n=%d  ss=%d  dd=%+d  bd=%+d",
        player_name, sport, stat_type,
        historical.overall.n, historical.sample_strength,
        data_delta, bet_delta,
    )

    return PropIntelligenceResult(
        historical            = historical,
        role                  = role,
        matchup               = matchup,
        sport_adapter         = adapter,
        data_confidence_delta = data_delta,
        betting_edge_delta    = bet_delta,
        intelligence_trace    = trace,
    )
