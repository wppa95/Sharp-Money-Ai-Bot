"""
engine/ — Sharp Money +EV Detection analysis modules.

Canonical import path for all analysis primitives:

    from engine.fair_probability import compute_fair_market, FairProbabilityMethod
    from engine.ev import compute_ev, compute_ev_batch, EVRating, ConfidenceFlag
    from engine.steam import compute_steam, compute_steam_simple, SteamResult, SteamTier
"""

from .fair_probability import (
    american_to_implied,
    implied_to_american,
    decimal_to_american,
    american_to_decimal,
    FairProbabilityMethod,
    FairProbabilityResult,
    FairMarket,
    compute_fair_probability,
    compute_fair_market,
    best_fair_market,
)

from .ev import (
    EVRating,
    ConfidenceFlag,
    EVResult,
    break_even_probability,
    expected_value_pct,
    edge_pct,
    fair_vs_market_diff,
    kelly_fraction,
    compute_ev,
    compute_ev_from_market,
    compute_ev_batch,
)

from .confidence import (
    ConfidenceTier,
    SupportingFactor,
    RiskWarning,
    ScoreBreakdown,
    ConfidenceResult,
    compute_confidence,
)

from .steam import (
    MovementDirection,
    SteamTier,
    ConfidenceLevel,
    LineMovementEvent,
    SteamMovement,
    SteamContext,
    SteamResult,
    SPORTSBOOK_WEIGHTS,
    SHARP_BOOK_THRESHOLD,
    compute_steam,
    compute_steam_simple,
)

from .analysis import AnalysisEngine
from .consensus import (
    compute_consensus,
    find_inefficiencies,
    build_multi_book_steam_inputs,
    ConsensusResult,
    MarketInefficiency,
)
from .clv import (
    compute_clv,
    build_clv_opportunity,
    CLVResult,
    CLVOpportunity,
)

__all__ = [
    # analysis
    "AnalysisEngine",
    # consensus
    "compute_consensus",
    "find_inefficiencies",
    "build_multi_book_steam_inputs",
    "ConsensusResult",
    "MarketInefficiency",
    # clv
    "compute_clv",
    "build_clv_opportunity",
    "CLVResult",
    "CLVOpportunity",
    # fair_probability
    "american_to_implied",
    "implied_to_american",
    "decimal_to_american",
    "american_to_decimal",
    "FairProbabilityMethod",
    "FairProbabilityResult",
    "FairMarket",
    "compute_fair_probability",
    "compute_fair_market",
    "best_fair_market",
    # ev
    "EVRating",
    "ConfidenceFlag",
    "EVResult",
    "break_even_probability",
    "expected_value_pct",
    "edge_pct",
    "fair_vs_market_diff",
    "kelly_fraction",
    "compute_ev",
    "compute_ev_from_market",
    "compute_ev_batch",
    # steam
    "MovementDirection",
    "SteamTier",
    "ConfidenceLevel",
    "LineMovementEvent",
    "SteamMovement",
    "SteamContext",
    "SteamResult",
    "SPORTSBOOK_WEIGHTS",
    "SHARP_BOOK_THRESHOLD",
    "compute_steam",
    "compute_steam_simple",
]
