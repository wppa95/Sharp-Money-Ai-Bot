"""
player_prop_market.py — Player Prop Market Comparison Engine.

This is the active alert framework for Sharp Money TeleBot. It replaces the
earlier "PrizePicks Reference Alert" model with a multi-provider market view
that surfaces the best available line across all data sources.

Supported providers (in priority order):
    🟣 PrizePicks  — primary (manual import via /pp_import; API is DataDome-blocked)
    🐶 Underdog    — secondary (live via UnderdogConnector)
    🎰 DraftKings  — tertiary (live via DraftKingsConnector when active)
    🦊 FanDuel     — tertiary (live via FanDuelConnector when active)

When a provider has no data for a given player/market, its line is shown as
"Unavailable" in the alert. The system continues using available sources.

Design principles:
  - "Best available line" = the highest-confidence live line regardless of source
  - Proxy Match Confidence measures provider agreement / proxy reliability —
    NOT betting confidence. Labelled explicitly as "Proxy Match Confidence".
  - Alert qualification tiers:
      S / A  — always qualify
      B+     — B-tier props with avg_vs_line ≥ 14/20 AND stability ≥ 10/15
      weak B — B-tier props that fail B+ criteria; suppressed (movement-only noise)
      PASS   — never alerted
  - Per-session dedup key: (player_name, sport, stat_type, line_str)
  - Old format_pp_reference_alert remains as dead code; this module owns the
    active alert path. No duplicate alert paths exist.
"""

from __future__ import annotations

import logging
import time
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

logger = logging.getLogger(__name__)

# ── Provider registry ─────────────────────────────────────────────────────────

@dataclass
class ProviderLine:
    """A single provider's line for one player/market combination."""
    provider:    str              # "PrizePicks" | "Underdog" | "DraftKings" | "FanDuel"
    emoji:       str              # visual identifier
    line_value:  Optional[float]  # None = unavailable
    available:   bool             # True if a line exists

    def display(self) -> str:
        if self.available and self.line_value is not None:
            return f"{self.line_value:.1f}"
        return "Unavailable"


# All providers the system is aware of, in display order.
PROVIDER_ORDER = ["PrizePicks", "Underdog", "DraftKings", "FanDuel"]
PROVIDER_EMOJI = {
    "PrizePicks": "🟣",
    "Underdog":   "🐶",
    "DraftKings": "🎰",
    "FanDuel":    "🦊",
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
    "3pm": "3-pointers made", "3-pointers made": "3-pointers made",
    "blk": "blocks", "blocks": "blocks",
    "stl": "steals", "steals": "steals",
    "pts+reb+ast": "pts+reb+ast",
    "fantasy points": "fantasy points",
    "hits": "hits", "hr": "home runs", "home runs": "home runs",
    "rbis": "rbis", "strikeouts": "strikeouts", "walks": "walks",
    "total bases": "total bases",
    "rushing yards": "rushing yards", "receiving yards": "receiving yards",
    "receptions": "receptions", "passing yards": "passing yards",
    "passing tds": "passing tds", "rush+rec yards": "rush+rec yards",
    "shots": "shots on goal", "shots on goal": "shots on goal",
    "goals+assists": "points",
    "kills": "kills", "deaths": "deaths", "maps won": "maps won",
    "games won": "games won", "sets won": "sets won",
}
_PP_CANONICAL_STATS: frozenset[str] = frozenset(_STAT_NORM.values())

PROXY_CONFIDENCE_THRESHOLD = 80

# ── B+ qualification gate ──────────────────────────────────────────────────────
# B-tier props (UDPropScore total 50–64) only qualify for alerts when their
# *quality* components are strong, not just their movement components.
#
# Movement-only B props (high vel/act, low avg/sta) produce noisy alerts with
# weak historical backing.  The B+ gate ensures a B prop has a meaningful
# historical deviation signal (avg_vs_line) AND consistent line values
# (stability) before it fires an alert.
#
# Thresholds chosen so that a prop at the top of the avg/sta scales always
# qualifies regardless of its movement score (e.g. 20/20 avg + 15/15 sta):
#   avg_vs_line ≥ 14 / 20  (70 % of max)
#   stability   ≥ 10 / 15  (67 % of max)
#
# To adjust, change these constants — do NOT change scoring weights.
_B_PLUS_AVG_MIN: int = 14   # avg_vs_line threshold (scored_props key "avg")
_B_PLUS_STA_MIN: int = 10   # stability threshold   (scored_props key "sta")


def _is_b_plus(p: dict) -> bool:
    """Return True if a B-tier scored_prop meets B+ alert-qualification criteria.

    Requires both:
      avg_vs_line ("avg") ≥ _B_PLUS_AVG_MIN  — strong historical deviation signal
      stability   ("sta") ≥ _B_PLUS_STA_MIN  — consistent line values over time

    Missing keys are treated as 0 (fails).  S/A/PASS callers should never
    reach this function — it is only meaningful for tier == "B".
    """
    avg = int(p.get("avg") or 0)
    sta = int(p.get("sta") or 0)
    return avg >= _B_PLUS_AVG_MIN and sta >= _B_PLUS_STA_MIN


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
    dk_line:        Optional[float] = None,     # DraftKings prop line if available
    fd_line:        Optional[float] = None,     # FanDuel prop line if available
    now:            Optional[datetime] = None,
    min_confidence: int = PROXY_CONFIDENCE_THRESHOLD,  # override for /picks (use 0 to show all)
) -> Optional[PlayerPropMarketComparison]:
    """
    Build a PlayerPropMarketComparison from available provider data.

    Returns None if proxy_match_confidence < min_confidence (default: PROXY_CONFIDENCE_THRESHOLD).
    Pass min_confidence=0 to build a comparison for all props regardless of score
    (useful for /picks display where confidence is shown as info, not a gate).
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
        "DraftKings": ProviderLine(
            provider   = "DraftKings",
            emoji      = PROVIDER_EMOJI["DraftKings"],
            line_value = dk_line,
            available  = dk_line is not None,
        ),
        "FanDuel": ProviderLine(
            provider   = "FanDuel",
            emoji      = PROVIDER_EMOJI["FanDuel"],
            line_value = fd_line,
            available  = fd_line is not None,
        ),
    }

    # ── Compute market view ───────────────────────────────────────────────────
    available_lines = [
        (pname, pl.line_value)
        for pname, pl in lines.items()
        if pl.available and pl.line_value is not None
    ]
    best_provider: Optional[str] = None
    best_line:     Optional[float] = None
    if available_lines:
        # Prefer PrizePicks > Underdog > DraftKings > FanDuel
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
    line movement, and proxy match confidence (labelled explicitly — not betting confidence).
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

    # ── Sources / confidence ──────────────────────────────────────────────────
    active_sources = [
        PROVIDER_EMOJI.get(p, p)
        for p in PROVIDER_ORDER
        if comp.lines.get(p) and comp.lines[p].available
    ]
    conf_bar = _conf_bar(comp.proxy_match_confidence)

    parts += [
        "",
        div,
        "",
        f"📡 <b>Sources:</b>  {' '.join(active_sources)}",
        f"🔬 <b>Proxy Match Confidence:</b>  {comp.proxy_match_confidence}/100  {conf_bar}",
        f"<i>Confidence measures provider agreement/proxy reliability, not betting edge.</i>",
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
) -> bool:
    """
    Return True when this prop alert should be suppressed.

    Suppressed when BOTH hold:
      • time since last alert < dedup_window_seconds
      • line moved < min_line_change since last alert

    A significant line movement always fires, even within the window.
    Accepts ``now_ts`` for test injection (defaults to ``time.time()``).
    """
    key = (player, sport, stat_type)
    if key not in alerted_set:
        return False
    last_ts, last_line = alerted_set[key]
    ts            = now_ts if now_ts is not None else time.time()
    within_window = (ts - last_ts) < dedup_window_seconds
    same_line     = abs(line - last_line) < min_line_change
    return within_window and same_line


def _record_prop_alerted(
    alerted_set: dict,
    player:      str,
    sport:       str,
    stat_type:   str,
    line:        float,
    now_ts:      Optional[float] = None,
) -> None:
    """Record that an alert was sent for (player, sport, stat_type, line)."""
    alerted_set[(player, sport, stat_type)] = (
        now_ts if now_ts is not None else time.time(),
        line,
    )


async def run_player_prop_market_cycle(
    *,
    db:            Any,
    bot:           Any,
    chat_ids:      list,
    scored_props:  list[dict],
    alerted_set:   dict,
    now:           Optional[datetime] = None,
    confidence_threshold: int = PROXY_CONFIDENCE_THRESHOLD,
) -> int:
    """
    Run the Player Prop Market Engine over the current cycle's high-scoring props.

    Qualification rules (see module docstring for B+ criteria):
      S / A  — always qualify
      B+     — B-tier with avg_vs_line ≥ 14/20 AND stability ≥ 10/15
      weak B — suppressed (movement-only noise)
      PASS   — never alerted

    For each qualifying candidate:
    1. Fetch PP PropLineHistory rows (one DB query for the batch).
    2. Build a PlayerPropMarketComparison.
    3. Broadcast a 🟣 PLAYER PROP MARKET ALERT if confidence >= threshold
       and the prop has not already been alerted in this session.

    Returns the number of alerts sent.
    """
    from alerts import broadcast_alert  # local to avoid circular

    if now is None:
        now = datetime.utcnow()

    def _admits(p: dict) -> bool:
        tier = p.get("tier", "")
        if tier in ("S", "A"):
            return True
        if tier == "B":
            qualifies = _is_b_plus(p)
            if qualifies:
                logger.debug(
                    "player_prop_market: B+ admitted — %s/%s  avg=%s sta=%s total=%s",
                    p.get("player"), p.get("stat_type"),
                    p.get("avg"), p.get("sta"), p.get("total"),
                )
            else:
                logger.debug(
                    "player_prop_market: B suppressed (weak) — %s/%s  avg=%s sta=%s total=%s",
                    p.get("player"), p.get("stat_type"),
                    p.get("avg"), p.get("sta"), p.get("total"),
                )
            return qualifies
        return False

    candidates = [p for p in scored_props if _admits(p)]
    if not candidates:
        logger.debug("player_prop_market: no qualifying props (S/A or B+) — skipping")
        return 0

    # One DB query for all PP history rows
    try:
        pp_rows = await db.get_latest_props_for_provider("PrizePicks", since_hours=24)
    except Exception as exc:
        logger.debug("player_prop_market: failed to fetch PP rows: %s", exc)
        pp_rows = []

    # Pre-fetch DK/FD player-prop OddsRecords for the whole cycle (1 query each)
    dk_fd_index: dict[tuple[str, str], float] = {}
    try:
        dk_fd_rows = await db.get_recent_player_prop_lines(
            ["DraftKings", "FanDuel"], since_hours=4
        )
        for rec in dk_fd_rows:
            sel      = (getattr(rec, "selection", None) or "").strip()
            line_val = getattr(rec, "line", None)
            if line_val is None:
                continue
            for suffix in (" Over", " Under"):
                if sel.endswith(suffix):
                    pkey = sel[: -len(suffix)].strip().lower()
                    key  = (pkey, rec.sportsbook)
                    if key not in dk_fd_index:
                        dk_fd_index[key] = float(line_val)
                    break
    except Exception as exc:
        logger.debug("player_prop_market: failed to fetch DK/FD rows: %s", exc)

    alerts_sent = 0
    for p in candidates:
        player    = p.get("player", "")
        stat_type = p.get("stat_type", "")
        sport     = p.get("sport", "")
        line      = float(p.get("line") or 0.0)
        prev_line = p.get("prev_line")   # may be None

        # Alert suppression — controlled by ALERT_DISABLED_SPORTS env var (default: NFL,NBA).
        # Data is still scored and stored; only the Telegram broadcast is skipped.
        if sport.upper() in _config.config.alert_disabled_sports:
            logger.debug("player_prop_market: alert suppressed (%s scope) — %s / %s", sport, player, stat_type)
            continue

        if _is_prop_deduped(
            alerted_set,
            player, sport, stat_type, line,
            dedup_window_seconds = _config.config.UD_ALERT_DEDUP_WINDOW,
            min_line_change      = _config.config.MIN_UNDERDOG_LINE_CHANGE,
        ):
            logger.debug(
                "player_prop_market: deduped — %s / %s / %s @ %.1f",
                player, stat_type, sport, line,
            )
            continue

        pkey    = player.lower()
        dk_line = dk_fd_index.get((pkey, "DraftKings"))
        fd_line = dk_fd_index.get((pkey, "FanDuel"))

        comp = build_player_prop_market_comparison(
            player_name   = player,
            sport         = sport,
            stat_type     = stat_type,
            ud_line       = line,
            previous_line = prev_line,
            fetched_at    = now,
            pp_rows       = pp_rows,
            dk_line       = dk_line,
            fd_line       = fd_line,
            now           = now,
        )
        if comp is None:
            continue

        try:
            message = format_player_prop_market_alert(comp)
            counts  = await broadcast_alert(bot, chat_ids, message)
            if counts.get("sent", 0) > 0:
                _record_prop_alerted(alerted_set, player, sport, stat_type, line)
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
            logger.warning(
                "player_prop_market: failed to send alert for %s / %s: %s",
                player, stat_type, exc,
            )

    return alerts_sent
