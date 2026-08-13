"""
market_engine.py — Multi-platform market engine background jobs.

Owns the polling jobs for:
  connector_poll_job      — fetch snapshots from all connectors, store, run consensus
  consensus_check_job     — run consensus engine, flag inefficiencies + multi-book steam
  clv_check_job           — detect CLV opportunities (current price > projected close)
  underdog_job            — fetch Underdog pick'em projections, alert on line changes

CLV design:
  - CLV *opportunities* (current price ahead of projected close) are alerted in
    clv_check_job.  They use MarketSnapshotRecord (alert_sent=True) for dedup.
    They are NOT stored as CLVRecord — there are no closing odds yet.
  - CLVRecord is reserved for *post-close* CLV results: compute_clv() called with
    real closing odds once the event starts.  /clv shows this history.

Steam dedup:
  After sending a multi-book steam alert a SteamRecord is persisted so that
  has_recent_steam_alert() correctly suppresses duplicates in the next cycle.

Underdog prop identity:
  The stat_type stored on UnderdogSnapshotRecord is the true stat category
  (e.g. "Fantasy Points"), extracted from the selection string produced by
  UnderdogConnector.  Change detection compares by player_name + stat_type,
  not by the raw selection string that includes the line value.

Pick'em isolation:
  Underdog snapshots (is_pickem=True) are never passed to sportsbook analysis.
"""

from __future__ import annotations

import gc
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import config
from engine.health import get_health_tracker
from engine.prop_intelligence import compute_prop_intelligence as _compute_intel
from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
from engine.score_validation import clamp_score
from database import (
    Database,
    MarketSnapshotRecord,
    CLVRecord,
    UnderdogSnapshotRecord,
    SteamRecord,
)
from connectors import ConnectorRegistry, MarketSnapshot
from engine.consensus import compute_consensus, find_inefficiencies, build_multi_book_steam_inputs
from engine.clv import build_clv_opportunity
from engine.steam import compute_steam_simple
from alerts import AlertDelivery, broadcast_alert, identify_sharp_books
from alerts_multiplatform import (
    format_steam_multibook_alert,
    format_inefficiency_alert,
    format_clv_opportunity_alert,
    format_underdog_change_alert,
    format_market_move_detected,  # noqa: F401 — imported for availability check
)

logger = logging.getLogger(__name__)

# Module-level registry — set by init_market_engine()
_registry: Optional[ConnectorRegistry] = None

# ── OddsAPI confirmation engine ───────────────────────────────────────────────
# Set once at startup via init_odds_confirmation(); used by
# _get_odds_api_confirmation() to call fetch_player_prop_lines().
_analysis_engine: Optional[object] = None


def init_odds_confirmation(engine: object) -> None:
    """Store the AnalysisEngine reference for OddsAPI player prop confirmation calls."""
    global _analysis_engine
    _analysis_engine = engine

# ── Player results integration ────────────────────────────────────────────────
# Singleton provider and per-day fetch dedup cache.
# Cache key: (player_name, sport, stat_type_lower, date_iso)
# The date component means stale entries are automatically bypassed next day.
_player_stats_provider = None
_player_result_fetch_cache: set = set()
# Maximum entries before the cache is wiped.  Each key is (player,sport,stat,date_iso);
# the date component means old entries are already bypassed by logic, but they
# accumulate in memory.  Clear the entire set once it grows past this ceiling so
# the next cycle re-fetches fresh data — a safe, cheap reset at ~300 s cadence.
_PLAYER_RESULT_CACHE_MAX = 5_000

# Set to True after the first complete Underdog prop scan.  The first cycle
# scores every active prop (cold-start mode); subsequent cycles use incremental
# scoring (new props and line-change events only).
_cold_start_done: bool = False

# ── Market availability tracking ─────────────────────────────────────────────
# Maps "player__stat_type" → datetime of first alert (bet pick).
# Used to compute how long a market was available before removal.
# Internal only — no Telegram alert is sent on removal (doc #4).
_MARKET_FIRST_ALERT: dict = {}
# Evict entries older than this many hours each scan cycle (prevents unbounded growth).
# Internal only — no Telegram alert is sent on removal (doc #4).
_MARKET_FIRST_ALERT_TTL_H: int = 24

# ── 95+ S-tier priority override ─────────────────────────────────────────────
# Tracks (player, sport, stat_type) tuples for which a 95+ override alert was
# already sent this session.  Persists across scan cycles; cleared on bot restart.
# Prevents the same exceptional prop from firing the override repeatedly.
_priority_override_sent: set = set()

# ── Underdog full-scan concurrency guard ──────────────────────────────────────
# Set to True while underdog_job is executing a full scan (fetch + score + deliver).
# A second instance (via max_instances=2) that finds this flag set will run only the
# fast new-prop detection path and return immediately, keeping the 2-minute polling
# cadence alive without duplicating the heavy scoring work.
_ud_full_scan_running: bool = False


def _bq_stars(bq: int) -> str:
    """V3.4 star display string computed from Bet Quality (decision.confidence).

    Mapping:
      100     → ★★★★★
      80–99   → ★★★★☆
      70–79   → ★★★☆☆
      40–69   → ★★☆☆☆
      0–39    → ★☆☆☆☆
    """
    if bq >= 100: n = 5
    elif bq >= 80: n = 4
    elif bq >= 70: n = 3
    elif bq >= 40: n = 2
    else: n = 1
    return "★" * n + "☆" * (5 - n)


# ---------------------------------------------------------------------------
# Market Quality actionable gate
# ---------------------------------------------------------------------------
from engine.ud_scoring import MarketQualityLabel
import typing


def _mq_allows_action(decision: Optional[object], market_quality: Optional[object]) -> tuple[bool, typing.Optional[str]]:
    """
    Targeted Market Quality gate for final actionable qualification.

    Returns (allowed: bool, reason: Optional[str]).

    Rules (per spec):
      - ELITE / HIGH : always allow
      - MEDIUM: A-tier requires >=2 supporting windows (games>=5) with
                OVER hit_rate >=0.55 or UNDER hit_rate <=0.45
      - LOW: S-tier always allow; B-tier preserved; A-tier allowed only when
             >=2 *strong* supporting windows (games>=5) with OVER hit_rate>=0.60
             or UNDER hit_rate<=0.40. Otherwise block.

    Uses only existing UDBetDecision fields (l5/l10/l20/l30/season games + hit_rate).
    """
    if market_quality is None or decision is None:
        return True, None
    mq_label = getattr(market_quality, "label", None)
    # Normalize enum
    try:
        if isinstance(mq_label, MarketQualityLabel):
            label = mq_label
        else:
            label = MarketQualityLabel(str(mq_label))
    except Exception:
        return True, None

    tier = getattr(decision, "decision_tier", None)
    rec = getattr(decision, "recommendation", None)

    # ELITE / HIGH: no gating
    if label in (MarketQualityLabel.ELITE, MarketQualityLabel.HIGH):
        return True, None

    # MEDIUM: A-tier requires 2 supporting windows at relaxed thresholds
    if label == MarketQualityLabel.MEDIUM:
        if tier == "A":
            support = 0
            for games, hit in (
                (getattr(decision, "l5_games", None), getattr(decision, "l5_hit_rate", None)),
                (getattr(decision, "l10_games", None), getattr(decision, "l10_hit_rate", None)),
                (getattr(decision, "l20_games", None), getattr(decision, "l20_hit_rate", None)),
                (getattr(decision, "l30_games", None), getattr(decision, "l30_hit_rate", None)),
                (getattr(decision, "season_games", None), getattr(decision, "season_hit_rate", None)),
            ):
                if games is None or hit is None:
                    continue
                if games < 5:
                    continue
                if rec == "OVER" and hit >= 0.55:
                    support += 1
                elif rec == "UNDER" and hit <= 0.45:
                    support += 1
            if support >= 2:
                return True, None
            return False, "MEDIUM_MQ_A_needs_2_supporting_windows"
        return True, None

    # LOW: S-tier allowed; B-tier preserved; A-tier allowed only with >=2 *strong* supports
    if label == MarketQualityLabel.LOW:
        if tier == "S":
            return True, None
        if tier == "B":
            return True, None
        if tier == "A":
            support = 0
            for games, hit in (
                (getattr(decision, "l5_games", None), getattr(decision, "l5_hit_rate", None)),
                (getattr(decision, "l10_games", None), getattr(decision, "l10_hit_rate", None)),
                (getattr(decision, "l20_games", None), getattr(decision, "l20_hit_rate", None)),
                (getattr(decision, "l30_games", None), getattr(decision, "l30_hit_rate", None)),
                (getattr(decision, "season_games", None), getattr(decision, "season_hit_rate", None)),
            ):
                if games is None or hit is None:
                    continue
                if games < 5:
                    continue
                if rec == "OVER" and hit >= 0.60:
                    support += 1
                elif rec == "UNDER" and hit <= 0.40:
                    support += 1
            if support >= 2:
                # Also ensure no strong contradicting window exists (reuse decision fields)
                for w_games, w_hit in (
                    (getattr(decision, "l5_games", None), getattr(decision, "l5_hit_rate", None)),
                    (getattr(decision, "l10_games", None), getattr(decision, "l10_hit_rate", None)),
                    (getattr(decision, "l20_games", None), getattr(decision, "l20_hit_rate", None)),
                    (getattr(decision, "l30_games", None), getattr(decision, "l30_hit_rate", None)),
                    (getattr(decision, "season_games", None), getattr(decision, "season_hit_rate", None)),
                ):
                    if w_games is None or w_hit is None:
                        continue
                    if w_games < 5:
                        continue
                    if rec == "OVER" and w_hit <= 0.40:
                        return False, "LOW_MQ_A_contradicted"
                    if rec == "UNDER" and w_hit >= 0.60:
                        return False, "LOW_MQ_A_contradicted"
                return True, None
            return False, "LOW_MQ_A_needs_2_strong_supporting_windows"
        return True, None

    return True, None

*** End Patch