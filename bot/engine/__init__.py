"""
engine/ — Sharp Money +EV Detection analysis modules.

Canonical import path for all analysis primitives:

    from engine.fair_probability import compute_fair_market, FairProbabilityMethod
    from engine.ev import compute_ev, compute_ev_batch, EVRating, ConfidenceFlag
    from engine.steam import compute_steam, compute_steam_simple, SteamResult, SteamTier
    from engine.ranking import compute_ranking, RankingResult, RankingTier, RankingDecision
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

from .ranking import (
    RankingTier,
    RankingDecision,
    HistoricalStats,
    HistoricalBreakdown,
    RankingResult,
    compute_ranking,
    MIN_SAMPLE_SIZE,
)

from .backtesting import (
    BacktestEngine,
    BacktestRecord,
    BacktestReport,
    DimensionStats,
    run_backtest,
)

from .season_check import SeasonChecker

# ── Framework v3.0 foundation layers ─────────────────────────────────────────

from .identity import (
    CanonicalPlayer,
    CanonicalMarket,
    CanonicalEvent,
    normalize_player_name,
    normalize_stat,
    player_key,
    event_key,
)

from .candidate import (
    ConfidenceDimensions,
    Candidate,
    VALID_DECISIONS,
    VALID_TIERS,
    VALID_RISK,
    candidate_from_ud_decision,
    candidate_from_ev_opportunity,
    candidate_from_alert_object,
    _intelligence_adjusted_tier,
)

from .explanation import (
    ExplanationFormat,
    ExplanationService,
    get_explanation_service,
)

# ── Framework v3.0 Layer 8 — Prop Intelligence ────────────────────────────────

from .prop_intelligence import (
    WindowStats,
    HistoricalIntelligence,
    RoleIntelligence,
    MatchupIntelligence,
    SportAdapter,
    PropIntelligenceResult,
    SPORT_ADAPTERS,
    get_sport_adapter,
    compute_historical_intelligence,
    compute_role_intelligence,
    compute_matchup_intelligence,
    compute_prop_intelligence,
    _sample_strength,            # exported for testing
)

__all__ = [
    # analysis
    "AnalysisEngine",
    # season check
    "SeasonChecker",
    # ranking / decision
    "RankingTier",
    "RankingDecision",
    "HistoricalStats",
    "HistoricalBreakdown",
    "RankingResult",
    "compute_ranking",
    "MIN_SAMPLE_SIZE",
    # backtesting
    "BacktestEngine",
    "BacktestRecord",
    "BacktestReport",
    "DimensionStats",
    "run_backtest",
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
    # confidence
    "ConfidenceTier",
    "SupportingFactor",
    "RiskWarning",
    "ScoreBreakdown",
    "ConfidenceResult",
    "compute_confidence",
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
