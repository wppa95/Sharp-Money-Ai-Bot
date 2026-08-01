"""
pp_reference.py — PrizePicks reference matching via Underdog props.

Since the PrizePicks API is DataDome-protected, this engine surfaces
Underdog pick'em projections as PrizePicks *reference* data. Underdog lines
are typically identical or within ±0.5 of PrizePicks for the same
player/market, making them a reliable proxy when no confirmed PP data exists.

IMPORTANT: All output from this engine is labelled as reference/proxy data.
PPReferenceMatch and format_pp_reference_alert must NEVER be presented as
confirmed PrizePicks data. The disclaimer is mandatory.

Design
──────
  1.  High-scoring Underdog props (tier S/A from the main scoring engine)
      are fed into match_underdog_to_pp() after the main alert loop.
  2.  Each prop is scored for PP-proxy confidence (0–100) based on four
      dimensions: player name quality, stat normalisation, sport support,
      and data recency.
  3.  Props that meet or exceed DEFAULT_CONFIDENCE_THRESHOLD (80) produce
      a PPReferenceMatch that can be formatted and broadcast.
  4.  If PropLineHistory contains actual PP rows (from manual import via
      /pp_import), they are used to boost confidence and improve the
      inferred PP line; otherwise Underdog data stands alone as the proxy.
  5.  Per-session dedup is handled by the caller (market_engine) via a
      module-level set keyed on (player_name, sport, stat_type, line_str).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import config as _config

logger = logging.getLogger(__name__)

# ── PrizePicks sport support list ─────────────────────────────────────────────

#: Sports that PrizePicks actively offers projections for.
PP_SUPPORTED_SPORTS: frozenset[str] = frozenset({
    "MLB", "NBA", "NFL", "NHL", "WNBA",
    "SOCCER", "TENNIS", "CS", "DOTA", "LOL",
})

# ── Stat normalisation ────────────────────────────────────────────────────────

#: Maps raw Underdog stat labels → canonical PP stat names.
#: Matching both lists reduces misses from minor naming differences.
_STAT_NORM: dict[str, str] = {
    # Basketball
    "pts":               "points",
    "points":            "points",
    "reb":               "rebounds",
    "rebounds":          "rebounds",
    "ast":               "assists",
    "assists":           "assists",
    "3pm":               "3-pointers made",
    "3-pointers made":   "3-pointers made",
    "blk":               "blocks",
    "blocks":            "blocks",
    "stl":               "steals",
    "steals":            "steals",
    "pts+reb+ast":       "pts+reb+ast",
    "fantasy points":    "fantasy points",
    # Baseball
    "hits":              "hits",
    "hr":                "home runs",
    "home runs":         "home runs",
    "rbis":              "rbis",
    "strikeouts":        "strikeouts",
    "walks":             "walks",
    "total bases":       "total bases",
    # American football
    "rushing yards":     "rushing yards",
    "receiving yards":   "receiving yards",
    "receptions":        "receptions",
    "passing yards":     "passing yards",
    "passing tds":       "passing tds",
    "rush+rec yards":    "rush+rec yards",
    # Hockey
    "shots":             "shots on goal",
    "shots on goal":     "shots on goal",
    "goals+assists":     "points",
    # Esports
    "kills":             "kills",
    "deaths":            "deaths",
    "maps won":          "maps won",
    # Tennis
    "games won":         "games won",
    "sets won":          "sets won",
}

#: Set of canonical PP stat names (values of _STAT_NORM).
_PP_CANONICAL_STATS: frozenset[str] = frozenset(_STAT_NORM.values())

# ── Confidence scoring constants ──────────────────────────────────────────────

#: Confidence breakdown (must sum to 100).
CONF_PLAYER_EXACT     = 40   # exact player name — non-empty, non-generic
CONF_STAT_NORMALISED  = 30   # stat type maps to a known PP canonical stat
CONF_SPORT_SUPPORTED  = 20   # sport is in PP_SUPPORTED_SPORTS
CONF_RECENCY          = 10   # data fetched within RECENCY_WINDOW_HOURS

RECENCY_WINDOW_HOURS  = 6    # hours within which data is considered "fresh"
DEFAULT_CONFIDENCE_THRESHOLD = 80


def normalize_stat_for_pp(raw: str) -> str:
    """Return the canonical PP stat name for *raw*, or *raw* lowercased if unknown."""
    return _STAT_NORM.get(raw.lower().strip(), raw.lower().strip())


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PPReferenceMatch:
    """
    A matched Underdog prop that qualifies as a PrizePicks reference.

    ``inferred_pp_line`` is the Underdog line value — PP lines are typically
    identical or within ±0.5.  The ±0.5 uncertainty is surfaced in the alert.

    ``pp_source`` distinguishes whether a live PP row from PropLineHistory was
    used ("prop_history_match") or whether Underdog stands alone ("underdog_proxy").
    """
    player_name:       str
    sport:             str
    stat_type:         str           # canonical PP stat name
    ud_line:           float         # Underdog line value
    inferred_pp_line:  float         # proxy PP line (= ud_line; PP typically identical)
    confidence:        int           # 0–100
    match_reason:      str           # human-readable explanation of scoring
    matched_at:        datetime
    pp_source:         str = "underdog_proxy"   # "underdog_proxy" | "prop_history_match"
    pp_line_from_db:   Optional[float] = None   # actual PP line if found in PropLineHistory


# ── Core matching function ────────────────────────────────────────────────────

def match_underdog_to_pp(
    player_name:        str,
    sport:              str,
    stat_type:          str,
    line_value:         float,
    fetched_at:         Optional[datetime] = None,
    *,
    prop_history_rows:  Optional[list] = None,  # list[PropLineHistory] — PP rows from DB
    now:                Optional[datetime] = None,
    confidence_threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
) -> Optional[PPReferenceMatch]:
    """
    Score an Underdog prop as a PrizePicks reference.

    Confidence breakdown (max 100):
      +40  Player name is clean and (if PP rows available) matches exactly
      +30  Stat type normalises to a canonical PP stat
      +20  Sport is in PP's active sport list
      +10  Data was fetched within the last 6 hours

    Returns a PPReferenceMatch when confidence >= confidence_threshold,
    or None if the prop does not qualify.

    Parameters
    ----------
    player_name, sport, stat_type, line_value:
        Core prop identity from the Underdog snapshot or scored-prop dict.
    fetched_at:
        When the Underdog data was fetched.  Used for recency scoring.
    prop_history_rows:
        PropLineHistory rows (provider="PrizePicks") fetched from the DB.
        If None or empty, Underdog data is used as a standalone proxy.
    now:
        Evaluation timestamp (defaults to utcnow()).
    confidence_threshold:
        Minimum confidence required to return a match.
    """
    if now is None:
        now = datetime.utcnow()

    score        = 0
    reasons: list[str] = []
    pp_source    = "underdog_proxy"
    pp_line_from_db: Optional[float] = None

    # ── Dimension 1: Player name (40 pts) ─────────────────────────────────────
    _bad_names = frozenset({"unknown", "", "n/a", "tbd", "tba"})
    player_clean = player_name.strip() if player_name else ""
    norm_stat_for_match = normalize_stat_for_pp(stat_type)   # used for PP row matching below
    if player_clean.lower() not in _bad_names:
        # Check against PropLineHistory PP rows if available.
        # Require player name, sport, AND normalised stat type to all match —
        # a player can have multiple markets and we must not cross-attach a
        # points line to a rebounds prop (or vice-versa).
        pp_rows = [
            r for r in (prop_history_rows or [])
            if getattr(r, "provider", "") == "PrizePicks"
            and getattr(r, "player_name", "").lower() == player_clean.lower()
            and getattr(r, "sport", "").upper() == sport.upper()
            and normalize_stat_for_pp(
                getattr(r, "stat_type", "")
            ) == norm_stat_for_match
        ]
        if pp_rows:
            # Sort by fetched_at descending to pick the newest compatible row.
            def _row_key(r):
                return getattr(r, "fetched_at", None) or datetime.min
            pp_rows_sorted = sorted(pp_rows, key=_row_key, reverse=True)
            best_row = pp_rows_sorted[0]
            score += CONF_PLAYER_EXACT
            pp_source = "prop_history_match"
            pp_line_from_db = float(best_row.line_value)
            reasons.append(f"PP history match: {player_clean} / {norm_stat_for_match}")
        else:
            # No matching PP row, but the name itself is clean — still qualifies.
            score += CONF_PLAYER_EXACT
            reasons.append(f"player: {player_clean}")
    else:
        # Degenerate player name — no points; likely bad data
        reasons.append("skip: unknown/empty player name")

    # ── Dimension 2: Normalised stat match (30 pts) ───────────────────────────
    norm_stat = normalize_stat_for_pp(stat_type)
    if norm_stat in _PP_CANONICAL_STATS:
        score += CONF_STAT_NORMALISED
        reasons.append(f"stat: {norm_stat}")
    elif stat_type and stat_type.lower().strip() not in _bad_names:
        # Known but unmapped stat — partial credit (not in PP canonical list)
        pass  # 0 pts — we only reward confirmed PP stats
    else:
        reasons.append("skip: unmapped stat type")

    # ── Dimension 3: Sport supported by PrizePicks (20 pts) ──────────────────
    if sport.upper() in PP_SUPPORTED_SPORTS:
        score += CONF_SPORT_SUPPORTED
        reasons.append(f"sport: {sport}")
    else:
        reasons.append(f"skip: sport {sport!r} not in PP list")

    # ── Dimension 4: Data recency (10 pts) ───────────────────────────────────
    if fetched_at is not None:
        age = now - fetched_at
        if age <= timedelta(hours=RECENCY_WINDOW_HOURS):
            score += CONF_RECENCY
            mins = int(age.total_seconds() // 60)
            reasons.append(f"fresh: {mins}m ago")
        else:
            reasons.append(f"stale: {int(age.total_seconds() // 3600)}h ago")
    else:
        # No timestamp — give benefit of the doubt
        score += CONF_RECENCY
        reasons.append("recency: unknown (credited)")

    score = min(score, 100)

    if score < confidence_threshold:
        logger.debug(
            "pp_reference: below threshold — %s / %s / %s  conf=%d < %d",
            player_clean, stat_type, sport, score, confidence_threshold,
        )
        return None

    # Infer PP line: typically identical; caller should surface ±0.5 uncertainty.
    inferred_pp_line = pp_line_from_db if pp_line_from_db is not None else line_value

    return PPReferenceMatch(
        player_name      = player_clean,
        sport            = sport,
        stat_type        = norm_stat or stat_type,
        ud_line          = line_value,
        inferred_pp_line = inferred_pp_line,
        confidence       = score,
        match_reason     = "; ".join(reasons),
        matched_at       = now,
        pp_source        = pp_source,
        pp_line_from_db  = pp_line_from_db,
    )


# ── Batch cycle helper (called from market_engine.underdog_job) ───────────────

async def run_pp_reference_cycle(
    *,
    db:            Any,          # Database
    bot:           Any,          # telegram.Bot
    chat_ids:      list,
    scored_props:  list[dict],   # _scored_props from underdog_job
    alerted_set:   dict,         # module-level dedup dict in market_engine
    now:           Optional[datetime] = None,
    confidence_threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
) -> int:
    """
    Run the PP reference engine over the current cycle's high-scoring props.

    Only props with tier in ("S", "A") are considered.  Each candidate is
    matched against PropLineHistory PP rows (one DB query for the full batch)
    and a reference alert is broadcast for every match that (a) meets the
    confidence threshold and (b) has not been alerted in this session.

    Returns the number of reference alerts sent.
    """
    from alerts import broadcast_alert, format_pp_reference_alert  # local to avoid circular

    if now is None:
        now = datetime.utcnow()

    # Filter to high-quality candidates only
    candidates = [
        p for p in scored_props
        if p.get("tier") in ("S", "A")
    ]
    if not candidates:
        logger.debug("pp_reference: no S/A tier props in this cycle — skipping")
        return 0

    # Fetch recent PP rows from PropLineHistory (one query for the whole batch)
    try:
        pp_rows = await db.get_latest_props_for_provider("PrizePicks", since_hours=24)
    except Exception as exc:
        logger.debug("pp_reference: failed to fetch PP history rows: %s", exc)
        pp_rows = []

    alerts_sent = 0
    for p in candidates:
        player    = p.get("player", "")
        stat_type = p.get("stat_type", "")
        sport     = p.get("sport", "")
        line      = float(p.get("line") or 0.0)

        _dedup_key  = (player, sport, stat_type)
        _now_ts     = time.time()
        if _dedup_key in alerted_set:
            _last_ts, _last_line = alerted_set[_dedup_key]
            _within_window = (_now_ts - _last_ts) < _config.config.UD_ALERT_DEDUP_WINDOW
            _same_line     = abs(line - _last_line) < _config.config.MIN_UNDERDOG_LINE_CHANGE
            if _within_window and _same_line:
                logger.debug(
                    "pp_reference: deduped — %s / %s / %s @ %.1f",
                    player, stat_type, sport, line,
                )
                continue

        match = match_underdog_to_pp(
            player_name         = player,
            sport               = sport,
            stat_type           = stat_type,
            line_value          = line,
            fetched_at          = now,   # treat as fresh (already filtered to current cycle)
            prop_history_rows   = pp_rows,
            now                 = now,
            confidence_threshold = confidence_threshold,
        )
        if match is None:
            continue

        try:
            message = format_pp_reference_alert(match)
            counts  = await broadcast_alert(bot, chat_ids, message)
            if counts.get("sent", 0) > 0:
                alerted_set[_dedup_key] = (_now_ts, line)
                alerts_sent += 1
                logger.info(
                    "pp_reference alert sent: %s / %s / %s  conf=%d  src=%s",
                    player, stat_type, sport, match.confidence, match.pp_source,
                )
        except Exception as exc:
            logger.warning(
                "pp_reference: failed to send alert for %s / %s: %s",
                player, stat_type, exc,
            )

    return alerts_sent
