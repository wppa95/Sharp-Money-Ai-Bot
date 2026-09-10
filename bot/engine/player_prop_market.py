"""
player_prop_market.py — Player Prop Market Comparison Engine.

This is the active alert framework for Sharp Money TeleBot. It replaces the
earlier "PrizePicks Reference Alert" model with a multi-provider market view
that surfaces the best available line across all data sources.

Supported providers (in priority order):
    🟣 PrizePicks  — primary (authorized PropLine provider)
    🐶 Underdog    — secondary (live via UnderdogConnector)

When a provider has no data for a given player/market, its line is shown as
"Unavailable" in the alert. The system continues using available sources.

Design principles:
  - "Best available line" = the highest-confidence live line regardless of source
  - Proxy Match Confidence measures provider agreement / proxy reliability —
    NOT betting confidence. Labelled explicitly as "Proxy Match Confidence".
  - Alert qualification requires a valid OVER/UNDER direction plus legitimate
    player-result evidence. Score, confidence, and legacy tier labels remain
    informational.
  - Per-session dedup key: (player_name, sport, stat_type, line_str)
  - Old format_pp_reference_alert remains as dead code; this module owns the
    active alert path. No duplicate alert paths exist.
"""

from __future__ import annotations

import logging
import time
from threading import Lock
from dataclasses import dataclass, field


def _line_label(line: float) -> str:
    """General line-level reference label — context only, not a difficulty rating."""
    if line <= 0.5:
        return "🟢 Low Line / Goblin Discount"
    elif line <= 1.5:
        return "⚪ Standard Line"
    else:
        return "🔴 Higher Difficulty Line"
import config as _config
from datetime import datetime, timedelta
from typing import Any, Optional
from engine.delivery_gates import directional_evidence_gate as _directional_evidence_gate
from engine.timing import is_game_alertable

logger = logging.getLogger(__name__)
_PROP_CLAIM_LOCK = Lock()

# ── Provider registry ─────────────────────────────────────────────────────────

@dataclass
class ProviderLine:
    """A single provider's line for one player/market combination."""
    provider:    str              # canonical provider name
    emoji:       str              # visual identifier
    line_value:  Optional[float]  # None = unavailable
    available:   bool             # True if a line exists

    def display(self) -> str:
        if self.available and self.line_value is not None:
            return f"{self.line_value:.1f}"
        return "Unavailable"


# Sportsbook rows are supplemental context for the Underdog pick, not separate
# alert sources. They are displayed only when the canonical ledger has a
# matching player/stat/event row.
PROVIDER_ORDER = ["PrizePicks", "Underdog", "DraftKings", "FanDuel"]
PROVIDER_EMOJI = {
    "PrizePicks": "🟣",
    "Underdog":   "🐶",
    "DraftKings": "🟦",
    "FanDuel":    "🔵",
}

# Sports that PrizePicks actively offers lines for
PP_SUPPORTED_SPORTS: frozenset[str] = frozenset({
    "MLB", "NBA", "NFL", "NHL", "WNBA",
    "SOCCER", "TENNIS", "CS", "DOTA", "LOL",
})

# Stat-type normalisation (UD abbrevs → canonical names)
_STAT_NORM: dict[str, str] = {
    "pts": "points", "points": "points",
    "reb": "rebounds", "rebounds": "rebounds",
    "ast": "assists", "assists": "assists",
    "3pm": "3-pointers made", "threes": "3-pointers made",
    "three pointers": "3-pointers made",
    "three pointers made": "3-pointers made",
    "3-pointers made": "3-pointers made",
    "blk": "blocks", "blocks": "blocks",
    "stl": "steals", "steals": "steals",
    "pts+reb+ast": "pts+reb+ast",
    "fantasy points": "fantasy points",
    "hits": "hits", "hr": "home runs", "home runs": "home runs",
    "rbis": "rbis", "strikeouts": "strikeouts", "walks": "walks",
    "total bases": "total bases",
    "runs": "runs", "stolen bases": "stolen bases",
    "hits + runs + rbis": "hits+runs+rbis",
    "batter walks": "walks",
    "rushing yards": "rushing yards", "receiving yards": "receiving yards",
    "receptions": "receptions", "passing yards": "passing yards",
    "passing tds": "passing tds", "rush+rec yards": "rush+rec yards",
    "shots": "shots on goal", "shots on goal": "shots on goal",
    "goals+assists": "points",
    "goals + assists": "points",
    "shots on target": "shots on target",
    "shots attempted": "shots attempted",
    "kills": "kills", "deaths": "deaths", "maps won": "maps won",
    "games won": "games won", "sets won": "sets won",
    "games played": "games played", "sets played": "sets played",
    "1st set games won": "1st set games won",
    "1st set games played": "1st set games played",
    "tiebreakers played": "tiebreakers played",
    "tie breakers played": "tiebreakers played",
    "aces": "aces", "double faults": "double faults",
    "points won": "points won", "breakpoints won": "breakpoints won",
    "1st set aces": "1st set aces",
    "significant strikes": "significant strikes",
    "fight time (mins)": "fight time (mins)",
    "takedowns": "takedowns",
    "finishing position": "finishing position",
}
_PP_CANONICAL_STATS: frozenset[str] = frozenset(_STAT_NORM.values())

PROXY_CONFIDENCE_THRESHOLD = 80

# Kept as read-only compatibility helpers for older callers and reports.
# They are not consulted by the active alert cycle.
_B_PLUS_AVG_MIN: int = 14
_B_PLUS_STA_MIN: int = 10


def _is_b_plus(p: dict) -> bool:
    """Return the legacy B+ classification without making it a gate."""
    return (
        int(p.get("avg") or 0) >= _B_PLUS_AVG_MIN
        and int(p.get("sta") or 0) >= _B_PLUS_STA_MIN
    )


def normalize_stat(raw: str) -> str:
    """Return the canonical stat name, falling back to lowercased raw."""
    return _STAT_NORM.get(raw.lower().strip(), raw.lower().strip())


# ── Core dataclass ────────────────────────────────────────────────────────────

@dataclass
class PlayerPropMarketComparison:
    """
    A multi-provider market view for one player × stat combination.

    Built from Underdog's current-cycle data, optionally cross-referenced
    against PropLineHistory PP rows and any sportsbook lines.

    ``proxy_match_confidence`` (0–100) measures provider agreement and proxy
    reliability — NOT betting confidence. Clearly labelled in every alert.
    """
    player_name:             str
    sport:                   str
    stat_type:               str              # canonical PP stat name
    lines:                   dict[str, ProviderLine]  # keyed by provider name
    best_provider:           Optional[str]    # provider with the priority-ordered best line
    best_line:               Optional[float]
    market_consensus:        Optional[float]  # average of all available lines
    previous_line:           Optional[float]  # previous Underdog line
    movement:                Optional[float]  # best_line - previous_line
    observed_at:             datetime
    proxy_match_confidence:  int              # 0–100, labelled in alert
    match_reason:            str              # human-readable scoring explanation
    # Best-available-app analysis (independent of provider priority)
    best_over_app:           Optional[str]    = None  # provider with lowest line (OVER-friendly)
    best_over_line:          Optional[float]  = None
    best_under_app:          Optional[str]    = None  # provider with highest line (UNDER-friendly)
    best_under_line:         Optional[float]  = None
    best_reason:             str              = ""    # e.g. "Market disagreement detected"


# ── Confidence scoring (mirrors pp_reference logic, renamed Proxy Match) ──────

def _compute_proxy_confidence(
    player_name:       str,
    sport:             str,
    stat_type:         str,
    fetched_at:        Optional[datetime],
    pp_rows:           list,
    now:               datetime,
) -> tuple[int, str, str]:
    """
    Score the Underdog prop as a PP market proxy.

    Returns (confidence, reason_string, pp_source).
      pp_source = "prop_history_match" | "underdog_proxy"
    """
    _bad = frozenset({"unknown", "", "n/a", "tbd", "tba"})
    player_clean  = (player_name or "").strip()
    norm_stat     = normalize_stat(stat_type)
    score         = 0
    reasons: list[str] = []
    pp_source     = "underdog_proxy"

    # Dimension 1: player name (40 pts)
    if player_clean.lower() not in _bad:
        matching_pp = [
            r for r in pp_rows
            if getattr(r, "provider", "") == "PrizePicks"
            and getattr(r, "player_name", "").lower() == player_clean.lower()
            and getattr(r, "sport", "").upper() == sport.upper()
            and normalize_stat(getattr(r, "stat_type", "")) == norm_stat
        ]
        if matching_pp:
            score     += 40    # cross-provider confirmed
            pp_source  = "prop_history_match"
            reasons.append(f"PP match: {player_clean}")
        else:
            score     += 20    # single-provider (Underdog only); proxy less confirmed
            reasons.append(f"player: {player_clean}")
    else:
        reasons.append("skip: unknown player")

    # Dimension 2: normalised stat (30 pts)
    if norm_stat in _PP_CANONICAL_STATS:
        score += 30
        reasons.append(f"stat: {norm_stat}")

    # Dimension 3: PP-supported sport (20 pts)
    if sport.upper() in PP_SUPPORTED_SPORTS:
        score += 20
        reasons.append(f"sport: {sport}")

    # Dimension 4: data freshness (10 pts)
    if fetched_at is not None:
        if (now - fetched_at) <= timedelta(hours=6):
            score += 10
            reasons.append("fresh data")
    else:
        score += 10   # unknown age — benefit of the doubt
        reasons.append("recency: unknown")

    return min(score, 100), "; ".join(reasons), pp_source


# ── Builder ───────────────────────────────────────────────────────────────────

def build_player_prop_market_comparison(
    player_name:    str,
    sport:          str,
    stat_type:      str,
    ud_line:        float,
    previous_line:  Optional[float] = None,
    fetched_at:     Optional[datetime] = None,
    pp_rows:        Optional[list] = None,      # PropLineHistory rows (provider=PrizePicks)
    dk_line:        Optional[float] = None,     # legacy compatibility; not displayed
    fd_line:        Optional[float] = None,     # legacy compatibility; not displayed
    now:            Optional[datetime] = None,
    min_confidence: int = PROXY_CONFIDENCE_THRESHOLD,  # override for /picks (use 0 to show all)
) -> Optional[PlayerPropMarketComparison]:
    """
    Build a PlayerPropMarketComparison from available provider data.

    ``min_confidence`` is an optional presentation filter for callers that
    explicitly request one.  The active alert cycle passes zero because proxy
    confidence is informational and cannot qualify or reject a directional
    pick.
    """
    if now is None:
        now = datetime.utcnow()

    norm_stat = normalize_stat(stat_type)

    # ── Compute proxy confidence ───────────────────────────────────────────────
    confidence, reason, pp_source = _compute_proxy_confidence(
        player_name  = player_name,
        sport        = sport,
        stat_type    = norm_stat,
        fetched_at   = fetched_at,
        pp_rows      = pp_rows or [],
        now          = now,
    )

    if confidence < min_confidence:
        logger.debug(
            "player_prop_market: below threshold — %s / %s / %s  conf=%d < %d",
            player_name, stat_type, sport, confidence, min_confidence,
        )
        return None

    # ── Provider lines ────────────────────────────────────────────────────────
    # PrizePicks: look for matching PP row in PropLineHistory
    pp_line_value: Optional[float] = None
    if pp_source == "prop_history_match" and pp_rows:
        matching = [
            r for r in pp_rows
            if getattr(r, "provider", "") == "PrizePicks"
            and getattr(r, "player_name", "").lower() == player_name.strip().lower()
            and getattr(r, "sport", "").upper() == sport.upper()
            and normalize_stat(getattr(r, "stat_type", "")) == norm_stat
        ]
        if matching:
            best = max(matching, key=lambda r: getattr(r, "fetched_at", None) or datetime.min)
            pp_line_value = float(best.line_value)

    lines: dict[str, ProviderLine] = {
        "PrizePicks": ProviderLine(
            provider   = "PrizePicks",
            emoji      = PROVIDER_EMOJI["PrizePicks"],
            line_value = pp_line_value,
            available  = pp_line_value is not None,
        ),
        "Underdog": ProviderLine(
            provider   = "Underdog",
            emoji      = PROVIDER_EMOJI["Underdog"],
            line_value = ud_line,
            available  = True,
        ),
    }
    # The current ledger may also contain sportsbook observations from the
    # Odds API player-prop ingestion job. Keep them informational: the
    # Underdog line remains the only actionable selection.
    for provider in ("DraftKings", "FanDuel"):
        matching = [
            row for row in (pp_rows or [])
            if getattr(row, "provider", "") == provider
            and getattr(row, "player_name", "").strip().lower()
                == player_name.strip().lower()
            and getattr(row, "sport", "").upper() == sport.upper()
            and normalize_stat(getattr(row, "stat_type", "")) == norm_stat
        ]
        latest = max(
            matching,
            key=lambda row: getattr(row, "fetched_at", None) or datetime.min,
            default=None,
        )
        book_line = (
            float(getattr(latest, "line_value"))
            if latest is not None and getattr(latest, "line_value", None) is not None
            else None
        )
        lines[provider] = ProviderLine(
            provider=provider,
            emoji=PROVIDER_EMOJI[provider],
            line_value=book_line,
            available=book_line is not None,
        )

    # ── Compute market view ───────────────────────────────────────────────────
    available_lines = [
        (pname, pl.line_value)
        for pname, pl in lines.items()
        if pl.available and pl.line_value is not None
    ]
    best_provider: Optional[str] = None
    best_line:     Optional[float] = None
    if available_lines:
        # Prefer PrizePicks over Underdog when both are available.
        for p in PROVIDER_ORDER:
            match = [(n, v) for n, v in available_lines if n == p]
            if match:
                best_provider, best_line = match[0]
                break

    consensus: Optional[float] = None
    if available_lines:
        vals = [v for _, v in available_lines]
        consensus = round(sum(vals) / len(vals) * 2) / 2   # round to nearest 0.5

    movement: Optional[float] = None
    if best_line is not None and previous_line is not None:
        movement = round(best_line - previous_line, 1)

    # ── Best Available App analysis ───────────────────────────────────────────
    # Independent of provider priority — computed from actual line values.
    # Lower line = OVER-friendly (easier to beat); higher = UNDER-friendly.
    best_over_app:   Optional[str]   = None
    best_over_line:  Optional[float] = None
    best_under_app:  Optional[str]   = None
    best_under_line: Optional[float] = None
    best_reason: str = ""

    if available_lines:
        over_sorted  = sorted(available_lines, key=lambda x: x[1])           # ascending
        under_sorted = sorted(available_lines, key=lambda x: -x[1])          # descending
        best_over_app,  best_over_line  = over_sorted[0]
        best_under_app, best_under_line = under_sorted[0]

        if len(available_lines) == 1:
            best_reason = "Only available provider"
        else:
            line_spread = best_under_line - best_over_line
            if line_spread >= 0.5:
                best_reason = "Market disagreement detected"
            elif line_spread == 0.0:
                best_reason = "All providers aligned"
            else:
                best_reason = "Lines closely aligned"

    return PlayerPropMarketComparison(
        player_name            = player_name.strip(),
        sport                  = sport,
        stat_type              = norm_stat or stat_type,
        lines                  = lines,
        best_provider          = best_provider,
        best_line              = best_line,
        market_consensus       = consensus,
        previous_line          = previous_line,
        movement               = movement,
        observed_at            = now,
        proxy_match_confidence = confidence,
        match_reason           = reason,
        best_over_app          = best_over_app,
        best_over_line         = best_over_line,
        best_under_app         = best_under_app,
        best_under_line        = best_under_line,
        best_reason            = best_reason,
    )


# ── Alert formatter ───────────────────────────────────────────────────────────

def format_player_prop_market_alert(comp: PlayerPropMarketComparison) -> str:
    """
    Format a Player Prop Market Alert for Telegram (HTML).

    Clearly shows all available providers, the best line, market consensus,
    line movement, and provider sources. Proxy match confidence remains an
    internal comparison value rather than Telegram presentation metadata.
    """
    sport_icons = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾", "NHL": "🏒",
        "UFC": "🥊", "WNBA": "🏀", "SOCCER": "⚽", "TENNIS": "🎾",
    }
    s_icon = sport_icons.get(comp.sport.upper(), "🎯")

    div   = "─" * 16
    thick = "━" * 18

    # ── Header ────────────────────────────────────────────────────────────────
    parts: list[str] = [
        thick,
        "🟣 <b>PLAYER PROP MARKET ALERT</b>",
        thick,
        "",
        f"{s_icon} <b>{comp.sport}</b>  ·  {comp.stat_type}",
        f"👤 <b>{comp.player_name}</b>",
        "",
        div,
        "",
        "📊 <b>Available Lines</b>",
        "",
    ]

    # ── Provider lines — only show providers with real data ───────────────────
    avail_lines = [
        (pname, comp.lines[pname])
        for pname in PROVIDER_ORDER
        if pname in comp.lines and comp.lines[pname].available
    ]
    if avail_lines:
        for pname, pl in avail_lines:
            parts.append(f"{pl.emoji} {pname}:  {pl.display()}")
    else:
        parts.append("No provider data available.")

    # ── Market view ───────────────────────────────────────────────────────────
    parts += [
        "",
        div,
        "",
        "📈 <b>Market View</b>",
        "",
    ]

    if comp.market_consensus is not None:
        parts.append(f"Market Consensus:     {comp.market_consensus:.1f}")
    else:
        parts.append("Market Consensus:     —")

    parts.append(f"Observed:             {comp.observed_at.strftime('%H:%M UTC')}")

    if comp.previous_line is not None:
        parts.append(f"Previous Line:        {comp.previous_line:.1f}")
    if comp.movement is not None:
        sign = "+" if comp.movement > 0 else ""
        arrow = "↑" if comp.movement > 0 else ("↓" if comp.movement < 0 else "→")
        parts.append(f"Movement:             <code>{sign}{comp.movement:.1f} {arrow}</code>")

    # ── Underdog Line ─────────────────────────────────────────────────────────
    parts += [
        "",
        div,
        "",
        "📊 <b>Underdog Line</b>",
        "",
    ]

    _avail_count = sum(
        1 for pl in comp.lines.values() if pl.available and pl.line_value is not None
    )
    if _avail_count == 0:
        parts.append("  No provider data available.")
    elif _avail_count == 1:
        # Only Underdog active — show line + context label
        pname  = comp.best_provider or "—"
        pline  = comp.best_line
        pemoji = PROVIDER_EMOJI.get(pname, "?")
        line_str = f"{pline:.1f}" if pline is not None else "—"
        parts.append(f"  {pemoji} <code>{line_str}</code>  {_line_label(pline) if pline is not None else ''}")
        parts.append(f"  <i>Reason: {comp.best_reason}</i>")
    else:
        # Multiple providers — show OVER-friendly and UNDER-friendly separately
        if comp.best_over_app and comp.best_over_line is not None:
            oe = PROVIDER_EMOJI.get(comp.best_over_app, "?")
            parts.append(
                f"  OVER-friendly:   {oe} <b>{comp.best_over_app}</b>"
                f"  <code>{comp.best_over_line:.1f}</code>  <i>(lowest line)</i>"
            )
        if comp.best_under_app and comp.best_under_line is not None:
            ue = PROVIDER_EMOJI.get(comp.best_under_app, "?")
            parts.append(
                f"  UNDER-friendly:  {ue} <b>{comp.best_under_app}</b>"
                f"  <code>{comp.best_under_line:.1f}</code>  <i>(highest line)</i>"
            )
        if comp.best_reason:
            parts.append(f"  <i>Reason: {comp.best_reason}</i>")
        parts.append("")
        parts.append(
            "  <i>Line differences = market information."
            " Not a betting recommendation.</i>"
        )

    # ── Sources ────────────────────────────────────────────────────────────────
    active_sources = [
        PROVIDER_EMOJI.get(p, p)
        for p in PROVIDER_ORDER
        if comp.lines.get(p) and comp.lines[p].available
    ]
    parts += [
        "",
        div,
        "",
        f"📡 <b>Sources:</b>  {' '.join(active_sources)}",
        "",
        thick,
    ]

    return "\n".join(parts)


def _conf_bar(confidence: int) -> str:
    filled = max(0, min(10, round(confidence / 10)))
    return f"<code>[{'█' * filled}{'░' * (10 - filled)}]</code>"


# ── Batch cycle helper ────────────────────────────────────────────────────────

# ── Alert dedup helpers ───────────────────────────────────────────────────────

def _is_prop_deduped(
    alerted_set:          dict,
    player:               str,
    sport:                str,
    stat_type:            str,
    line:                 float,
    dedup_window_seconds: int,
    min_line_change:      float,
    now_ts:               Optional[float] = None,
    *,
    provider:             Optional[str] = None,
    source_id:            Optional[str] = None,
    event_id:             Optional[str] = None,
    direction:            Optional[str] = None,
) -> bool:
    """
    Return True when this prop alert should be suppressed.

    Suppressed when BOTH hold:
      • time since last alert < dedup_window_seconds
      • line moved < min_line_change since last alert

    A significant line movement always fires, even within the window.
    Accepts ``now_ts`` for test injection (defaults to ``time.time()``).
    """
    # Keep the compact legacy key for old callers/tests.  Production delivery
    # paths pass the complete provider-owned identity so the same player/stat
    # can alert independently on different providers, events, lines, and sides.
    identity = (
        str(provider or "").strip(),
        str(source_id or "").strip(),
        str(event_id or "").strip(),
        str(direction or "").strip().upper(),
    )
    key = (
        (player, sport, stat_type, *identity)
        if any(identity)
        else (player, sport, stat_type)
    )
    ts            = now_ts if now_ts is not None else time.time()

    def _matches_recent(value: object) -> bool:
        """Apply the time/line rules to one stored alert value."""
        try:
            last_ts, last_line = value
            within_window = (ts - float(last_ts)) < dedup_window_seconds
            same_line = abs(float(line) - float(last_line)) < min_line_change
        except (TypeError, ValueError):
            return False
        return within_window and same_line

    if key in alerted_set and _matches_recent(alerted_set[key]):
        return True

    # Source/event identifiers are useful audit metadata, but they can rotate
    # while the logical pick remains the same.  Once a direction is known, use
    # the stable provider × player × sport × market × direction identity as a
    # fallback.  This prevents repeated MLB alerts after an Underdog refresh
    # without collapsing PrizePicks and Underdog into one delivery slot.
    if direction:
        logical_prefix = (player, sport, stat_type)
        normalized_direction = str(direction).strip().upper()
        normalized_provider = str(provider or "").strip()
        for stored_key, stored_value in alerted_set.items():
            if not isinstance(stored_key, tuple) or len(stored_key) < 7:
                continue
            if tuple(stored_key[:3]) != logical_prefix:
                continue
            if str(stored_key[3]).strip() != normalized_provider:
                continue
            if str(stored_key[6]).strip().upper() != normalized_direction:
                continue
            if _matches_recent(stored_value):
                return True
    return False


def _record_prop_alerted(
    alerted_set: dict,
    player:      str,
    sport:       str,
    stat_type:   str,
    line:        float,
    now_ts:      Optional[float] = None,
    *,
    provider:    Optional[str] = None,
    source_id:   Optional[str] = None,
    event_id:    Optional[str] = None,
    direction:   Optional[str] = None,
) -> None:
    """Record an alert using its provider-owned source identity."""
    identity = (
        str(provider or "").strip(),
        str(source_id or "").strip(),
        str(event_id or "").strip(),
        str(direction or "").strip().upper(),
    )
    key = (
        (player, sport, stat_type, *identity)
        if any(identity)
        else (player, sport, stat_type)
    )
    alerted_set[key] = (
        now_ts if now_ts is not None else time.time(),
        line,
    )

def _release_prop_delivery_claim(
    alerted_set: dict,
    player: str,
    sport: str,
    stat_type: str,
    line: float,
    *,
    provider: Optional[str] = None,
    source_id: Optional[str] = None,
    event_id: Optional[str] = None,
    direction: Optional[str] = None,
) -> None:
    """Release a pre-delivery dedup claim when Telegram delivery fails."""
    identity = (
        str(provider or "").strip(),
        str(source_id or "").strip(),
        str(event_id or "").strip(),
        str(direction or "").strip().upper(),
    )
    key = (
        (player, sport, stat_type, *identity)
        if any(identity)
        else (player, sport, stat_type)
    )
    current = alerted_set.get(key)

    if current is not None:
        _last_ts, _last_line = current
        if abs(float(_last_line) - float(line)) < 1e-9:
            alerted_set.pop(key, None)


def _try_claim_prop_delivery(
    alerted_set: dict,
    player: str,
    sport: str,
    stat_type: str,
    line: float,
    *,
    dedup_window_seconds: int,
    min_line_change: float,
    provider: Optional[str] = None,
    source_id: Optional[str] = None,
    event_id: Optional[str] = None,
    direction: Optional[str] = None,
) -> bool:
    """Atomically check and pre-claim a compatibility delivery slot."""
    with _PROP_CLAIM_LOCK:
        if _is_prop_deduped(
            alerted_set,
            player,
            sport,
            stat_type,
            line,
            dedup_window_seconds=dedup_window_seconds,
            min_line_change=min_line_change,
            provider=provider,
            source_id=source_id,
            event_id=event_id,
            direction=direction,
        ):
            return False
        _record_prop_alerted(
            alerted_set,
            player,
            sport,
            stat_type,
            line,
            provider=provider,
            source_id=source_id,
            event_id=event_id,
            direction=direction,
        )
        return True


async def run_player_prop_market_cycle(
    *,
    db:            Any,
    bot:           Any,
    chat_ids:      list,
    scored_props:  list[dict],
    alerted_set:   dict,
    now:           Optional[datetime] = None,
    confidence_threshold: int = 0,
) -> int:
    """
    Run the Player Prop Market Engine over the current cycle's high-scoring props.

    Qualification requires the canonical OVER/UNDER plus evidence gate.
    Score, proxy confidence, and legacy tier labels are informational.

    For each qualifying candidate:
    1. Fetch PP PropLineHistory rows (one DB query for the batch).
    2. Build a PlayerPropMarketComparison.
    3. Broadcast a 🟣 PLAYER PROP MARKET ALERT when the comparison can be
       built and the prop has not already been alerted in this session.

    Returns the number of alerts sent.
    """
    from alerts import _is_fresh_delivery_line, broadcast_alert  # local to avoid circular

    if now is None:
        now = datetime.utcnow()

    def _admits(p: dict) -> bool:
        return (p.get("direction") or "").strip().upper() in {"OVER", "UNDER"}

    candidates = [p for p in scored_props if _admits(p)]
    if not candidates:
        logger.debug("player_prop_market: no directional props — skipping")
        return 0

    # One DB query for all PP history rows
    try:
        pp_rows = await db.get_latest_props_for_provider("PrizePicks", since_hours=24)
    except Exception as exc:
        logger.debug("player_prop_market: failed to fetch PP rows: %s", exc)
        pp_rows = []

    alerts_sent = 0
    for p in candidates:
        player    = p.get("player", "")
        stat_type = p.get("stat_type", "")
        sport     = p.get("sport", "")
        line      = float(p.get("line") or 0.0)
        prev_line = p.get("prev_line")   # may be None
        provider = str(p.get("provider") or "PrizePicks").strip()
        if provider.upper() not in {"UNDERDOG", "PRIZEPICKS"}:
            logger.debug(
                "player_prop_market: invalid provider blocked — %s / %s / %s",
                provider, player, stat_type,
            )
            continue
        fetched_at = p.get("fetched_at")
        if not _is_fresh_delivery_line(fetched_at):
            logger.debug(
                "player_prop_market: stale provider line blocked — %s / %s / %s",
                player, stat_type, provider,
            )
            continue
        # Preserve the compact legacy key for old callers that do not provide
        # source/event identity, but use the provider-owned identity whenever
        # the scored observation has one.  This keeps alternate lines and
        # separate events/providers from cross-suppressing one another.
        source_id = p.get("source_id") or p.get("external_id") or ""
        event_id = p.get("event_id") or p.get("game_id") or ""
        dedup_identity = {}
        if source_id or event_id:
            dedup_identity = {
                "provider": provider,
                "source_id": source_id,
                "event_id": event_id,
                "direction": p.get("direction") or "",
            }

        # Optional operator suppression controlled by ALERT_DISABLED_SPORTS
        # (empty by default). Sport alone is otherwise never a delivery veto.
        # Data is still scored and stored; only the Telegram broadcast is skipped.
        if sport.upper() in _config.config.alert_disabled_sports:
            logger.debug("player_prop_market: alert suppressed (%s scope) — %s / %s", sport, player, stat_type)
            continue

        _evidence_available = bool(
            p.get("evidence_available")
            or getattr(p.get("validation"), "has_supporting_data", False)
        )
        if not _directional_evidence_gate(
            str(p.get("direction") or ""),
            evidence_available=_evidence_available,
        ):
            logger.debug(
                "player_prop_market: canonical gate blocked — %s / %s / %s",
                player, stat_type, sport,
            )
            continue

        timing_allowed, timing_reason = is_game_alertable(
            p.get("game_time"),
            min_minutes=0,
            max_minutes=10**9,
            urgent_edge=0,
            edge=0,
            game_status=p.get("game_status") or p.get("provider_status"),
            require_known=True,
        )
        if not timing_allowed:
            logger.debug(
                "player_prop_market: timing blocked — %s / %s / %s: %s",
                player, stat_type, sport, timing_reason,
            )
            continue

        comp = build_player_prop_market_comparison(
            player_name   = player,
            sport         = sport,
            stat_type     = stat_type,
            ud_line       = line,
            previous_line = prev_line,
            fetched_at    = fetched_at if isinstance(fetched_at, datetime) else now,
            pp_rows       = pp_rows,
            min_confidence = confidence_threshold,
            now           = now,
        )
        if comp is None:
            continue

        if not _try_claim_prop_delivery(
            alerted_set,
            player, sport, stat_type, line,
            dedup_window_seconds = _config.config.UD_ALERT_DEDUP_WINDOW,
            min_line_change      = _config.config.MIN_UNDERDOG_LINE_CHANGE,
            **dedup_identity,
        ):
            logger.debug(
                "player_prop_market: deduped — %s / %s / %s @ %.1f",
                player, stat_type, sport, line,
            )
            continue

        try:
            message = format_player_prop_market_alert(comp)
            counts  = await broadcast_alert(bot, chat_ids, message, sport=sport)
            if counts.get("sent", 0) <= 0:
                _release_prop_delivery_claim(
                    alerted_set, player, sport, stat_type, line, **dedup_identity
                )
            else:
                alerts_sent += 1
                logger.info(
                    "player_prop_market alert sent: %s / %s / %s  "
                    "conf=%d  best=%s(%.1f)",
                    player, stat_type, sport,
                    comp.proxy_match_confidence,
                    comp.best_provider or "?",
                    comp.best_line or 0.0,
                )
        except Exception as exc:
            _release_prop_delivery_claim(
                alerted_set, player, sport, stat_type, line, **dedup_identity
            )
            logger.warning(
                "player_prop_market: failed to send alert for %s / %s: %s",
                player, stat_type, exc,
            )

    return alerts_sent
