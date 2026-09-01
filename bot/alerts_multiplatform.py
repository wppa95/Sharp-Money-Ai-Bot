"""
alerts_multiplatform.py — Alert formatters for multi-platform market engine.

New alert types:
  format_steam_multibook_alert  — coordinated steam across DraftKings/FanDuel/etc.
  format_inefficiency_alert     — one book deviating from cross-book consensus
  format_clv_opportunity_alert  — current price better than projected close
  format_clv_result_alert       — CLV result after event closes
  format_underdog_change_alert  — Underdog line change or removed prop
  format_underdog_new_prop_alert — first-appearance Underdog prop
"""

from __future__ import annotations

from datetime import datetime
from html import escape
import re
from typing import Optional


def _format_game_time_et(dt: datetime) -> str:
    """Convert a naive-UTC datetime to a readable 12-hour ET string.

    Returns e.g. ``"8:40 PM ET"`` for Telegram display.
    Falls back to ``"HH:MM UTC"`` if the zoneinfo conversion fails.
    UTC is kept internally everywhere; this is display-only.
    """
    try:
        from zoneinfo import ZoneInfo
        dt_et = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        # %-I strips the leading zero on Linux ("8:40 PM ET" not "08:40 PM ET")
        return dt_et.strftime("%-I:%M %p ET")
    except Exception:
        return dt.strftime("%H:%M UTC")


def _line_label(line: float) -> str:
    """General line-level reference label — context only, not a difficulty rating."""
    if line <= 0.5:
        return "🟢 Low Line / Goblin Discount"
    elif line <= 1.5:
        return "⚪ Standard Line"
    else:
        return "🔴 Higher Difficulty Line"

from alerts import EMOJI, _div, format_odds, format_probability, RiskFactor
from engine.consensus import ConsensusResult, MarketInefficiency
from engine.clv import CLVOpportunity, CLVResult

# ── V3.4 Bet Quality star + priority helpers ───────────────────────────────────

_BQ_STAR_BANDS = ((100, 5), (80, 4), (70, 3), (40, 2))  # (min_bq, stars)

def _bq_stars(bq: int) -> str:
    """Star display from Bet Quality (decision.confidence).

    V3.4 mapping (independent of raw market score):
      100     → ★★★★★
      80–99   → ★★★★☆
      70–79   → ★★★☆☆
      40–69   → ★★☆☆☆
      0–39    → ★☆☆☆☆
    """
    n = 1
    for min_bq, stars in _BQ_STAR_BANDS:
        if bq >= min_bq:
            n = stars
            break
    return "★" * n + "☆" * (5 - n)


def _bq_priority_label(bq: int, direction: str = "", sport: str = "") -> str:
    """Priority/actionability label for a given Bet Quality score.

    These labels remain available to raw formatter/unit-test callers.  The
    Telegram and Discord delivery boundary sanitizes retired classifications
    before any message leaves the process.
    """
    _tier2_sport = sport.upper() in {"MLB", "NFL"}
    if bq >= 80:
        if direction == "UNDER":
            return "💪 STRONG BET" if _tier2_sport else "💪 STRONG UNDER"
        return "💪 STRONG BET"
    elif bq >= 75:
        return "🟢 ACTIONABLE"
    elif bq >= 70:
        # Direction is the only qualification gate.  Do not expose the
        # retired watchlist classification in raw formatter output.
        return ""
    return ""


# ── Multi-book steam alert ─────────────────────────────────────────────────────

def format_steam_multibook_alert(
        event: str,
        selection: str,
        sport: str,
        market_type: str,
        steam_score: int,
        steam_direction: str,
        books_moved: list[str],
        opening_odds: int,
        current_odds: int,
        *,
        consensus_odds: Optional[int] = None,
        
        sharp_books: Optional[list[str]] = None,
        risk_factors: Optional[list[RiskFactor]] = None,
    ) -> str:
    """
    Format a cross-platform steam alert spanning DraftKings, FanDuel, and
    other registered sportsbooks. Shows book attribution alongside the score.
    """
    from alerts import identify_sharp_books, compute_steam_risk_factors
    from models import AlertType, SteamAlert, Sport, MarketType

    if sharp_books is None:
        sharp_books = identify_sharp_books(books_moved)

    if risk_factors is None:
        risk_factors = compute_steam_risk_factors(
            books_moved,
            steam_score,
            sharp_books,
        )

    dir_icon  = EMOJI["down"] if steam_direction == "DOWN" else EMOJI["up"]
    dir_label = "FALLING" if steam_direction == "DOWN" else "RISING"
    books_str = ", ".join(books_moved) if books_moved else "—"
    sharp_str = ", ".join(sharp_books) if sharp_books else "None detected"
    change    = current_odds - opening_odds
    filled    = round(steam_score / 10)
    score_bar = "█" * filled + "░" * (10 - filled)

    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(sport, "🎯")

    consensus_line = (
        f"\n  📊 Consensus:  <code>{format_odds(consensus_odds)}</code>"
        if consensus_odds is not None
        else ""
    )

    if risk_factors is None:
        from alerts import RiskFactor as RF
        risk_factors = []
        if len(books_moved) < 3:
            risk_factors.append(RF("MEDIUM", f"Only {len(books_moved)} books — limited confirmation"))
        if not sharp_books:
            risk_factors.append(RF("MEDIUM", "No sharp books in movers"))
        if steam_score < 70:
            risk_factors.append(RF("LOW", f"Moderate steam score ({steam_score}/100)"))

    parts = [
        f"🔥 <b>MULTI-BOOK STEAM ALERT</b> 🔥",
        "",
        f"{sport_icon} <b>{sport}</b>  ·  {market_type}",
        f"📋 <b>{event}</b>",
        "",
        _div(),
        f"{dir_icon} <b>{selection}</b>  —  odds {dir_label}",
        f"  Opening:   <code>{format_odds(opening_odds)}</code>",
        f"  Current:   <code>{format_odds(current_odds)}</code>",
        f"  Change:    <code>{format_odds(change)}</code>{consensus_line}",
        _div(),
        "",
        f"{EMOJI['fire']} <b>Steam Score:  {steam_score}/100</b>",
        f"  <code>[{score_bar}]</code>",
        "",
        f"📚 <b>Books Moved</b>  ({len(books_moved)})",
        f"  All:      {books_str}",
        f"  ⚡ Sharp:  {sharp_str}",
        "",
        _div(),
        *_risk_section(risk_factors),
        "",
        f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(parts)


# ── Market inefficiency alert ──────────────────────────────────────────────────

def format_inefficiency_alert(
        ineff: MarketInefficiency,
        consensus: ConsensusResult,
        *,
        risk_factors: Optional[list[RiskFactor]] = None,
    ) -> str:
    """
    Format an alert for a book offering better-than-consensus odds.
    Positive deviation = value opportunity (book hasn't moved yet).
    """
    sport_icon = { 
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(ineff.sport, "🎯")

    sign = "+" if ineff.deviation > 0 else ""
    consensus_str = format_odds(ineff.consensus_odds)
    offered_str   = format_odds(ineff.offered_odds)

    value_label = "📈 BOOK LAGGING CONSENSUS" if ineff.is_value else "📉 BOOK AHEAD OF CONSENSUS"

    if risk_factors is None:
        from alerts import RiskFactor as RF
        risk_factors = []
        if consensus.book_count < 3:
            risk_factors.append(RF("MEDIUM", f"Only {consensus.book_count} books in consensus — thin sample"))
        if ineff.abs_deviation < 15:
            risk_factors.append(RF("LOW", f"Small deviation ({ineff.abs_deviation}¢) — may close quickly"))

    parts = [
        f"📊 <b>MARKET INEFFICIENCY DETECTED</b>",
        "",
        f"{sport_icon} <b>{ineff.sport}</b>  ·  {ineff.market_type}",
        f"📋 <b>{ineff.event}</b>",
        "",
        _div(),
        f"{value_label}",
        f"  <b>Book:</b>       {ineff.sportsbook}",
        f"  <b>Selection:</b>  {ineff.selection}",
        f"  <b>Offered:</b>    <code>{offered_str}</code>",
        f"  <b>Consensus:</b>  <code>{consensus_str}</code>",
        f"  <b>Deviation:</b>  <code>{sign}{ineff.deviation}</code> cents",
        "",
        f"📚 <b>Books in consensus:</b> {consensus.book_count}",
        f"  Range: <code>{format_odds(consensus.min_odds)}</code> – <code>{format_odds(consensus.max_odds)}</code>",
        f"  Others: {', '.join(b for b in consensus.books if b != ineff.sportsbook)}",
        "",
        _div(),
        "",
        f"{EMOJI['clock']} <i>{ineff.detected_at.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(parts)


# ── CLV opportunity alert (current price ahead of projected close) ─────────────

def format_clv_opportunity_alert(
    opp: CLVOpportunity,
    *,
    risk_factors: Optional[list[RiskFactor]] = None,
) -> str:
    """
    Format an alert when a book's current price is better than the projected
    closing line — act now before it closes.
    """
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(opp.sport, "🎯")

    curr_str  = format_odds(opp.current_odds)
    close_str = format_odds(opp.projected_close)
    lead_sign = "+" if opp.clv_lead >= 0 else ""

    if risk_factors is None:
        from alerts import RiskFactor as RF
        risk_factors = []
        if opp.books_count < 3:
            risk_factors.append(RF("MEDIUM", f"Projected close from {opp.books_count} books — thin sample"))
        if opp.clv_lead < 10:
            risk_factors.append(RF("LOW", f"Modest lead ({opp.clv_lead}¢) — may close before action"))

    parts = [
        f"💎 <b>CLV OPPORTUNITY — ACT NOW</b>",
        "",
        f"{sport_icon} <b>{opp.sport}</b>  ·  {opp.market_type}",
        f"📋 <b>{opp.event}</b>",
        "",
        _div(),
        f"⚡ <b>{opp.selection}</b>",
        f"  <b>Book:</b>            {opp.sportsbook}",
        f"  <b>Current Price:</b>   <code>{curr_str}</code>",
        f"  <b>Projected Close:</b> <code>{close_str}</code>",
        f"  <b>CLV Lead:</b>        <code>{lead_sign}{opp.clv_lead}</code> cents ahead",
        "",
        f"📚 <b>Consensus from {opp.books_count} books</b>",
        f"<i>Book hasn't moved yet — price should close closer to {close_str}</i>",
        "",
        _div(),
        "",
        f"{EMOJI['clock']} <i>{opp.detected_at.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(parts)


# ── CLV result alert (post-close performance) ──────────────────────────────────

def format_clv_result_alert(result: CLVResult) -> str:
    """Format a CLV result for a bet/alert that has now closed."""
    sign    = "+" if result.clv_pct >= 0 else ""
    clv_str = f"{sign}{result.clv_pct:.2f}%"
    beat    = "✅ Beat the close" if result.beat_close else "❌ Missed the close"

    parts = [
        f"{result.clv_emoji} <b>CLV RESULT — {beat}</b>",
        "",
        f"⚡ <b>{result.selection}</b>",
        "",
        _div(),
        f"  <b>Bet Odds:</b>    <code>{format_odds(result.bet_odds)}</code>",
        f"  <b>Closing Odds:</b> <code>{format_odds(result.closing_odds)}</code>",
        f"  <b>CLV:</b>          <code>{clv_str}</code>  ({result.clv_grade})",
        f"  <b>Odds Shift:</b>   <code>{'+' if result.clv_proxy >= 0 else ''}{result.clv_proxy}</code> cents",
    ]

    if result.notes:
        parts += ["", f"<i>{result.notes}</i>"]

    parts += [
        "",
        f"{EMOJI['clock']} <i>{result.computed_at.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(parts)


# ── Underdog line change alert ─────────────────────────────────────────────────

def _format_decision_block(
    decision: Optional[object],
    line: Optional[float] = None,
) -> str:
    """
    Format the full OVER / UNDER evaluation block for Underdog alerts.

    Shows confidence, all per-window hit-rate statistics, and a reason.
    When *line* is supplied and the recommendation is OVER or UNDER, a
    "📌 Suggested Play: OVER/UNDER X.X" line is added immediately after the
    recommendation so the actionable play is always visible at a glance.
    Returns an empty string when *decision* is None.
    """
    if decision is None:
        return ""

    rec       = getattr(decision, "recommendation", "") or ""
    if rec not in ("OVER", "UNDER"):
        # Non-directional evaluations are internal scan results, not alerts.
        return ""
    emoji     = getattr(decision, "recommendation_emoji", lambda: "⚪")()
    reason = _clean_reason(getattr(decision, "reason", ""))

    play_line = (
        f"\n📌 <b>Suggested Play:</b> {rec} <code>{line:.1f}</code>"
        if line is not None else ""
    )
    reason_block = (
        f"\n\n💬 <b>Reason:</b>\n   <i>{reason}</i>"
        if reason else ""
    )

    return (
        f"\n\n{_div()}"
        f"\n🎯 <b>Recommendation:</b>  {emoji} {rec}"
        f"{play_line}"
        f"\n   {_format_quality_line(decision, None)}"
        f"{_format_history_block(decision)}"
        f"{reason_block}"
    )


def _format_market_quality_block(market_quality: Optional[object]) -> str:
    """Render the Market Quality section for Underdog alerts (informational).

    Market Quality = How reliable is the market data?
    This is separate from Bet Quality (how strong is the actual recommendation).
    """
    if market_quality is None:
        return ""
    label   = getattr(market_quality, "label",   None)
    score   = getattr(market_quality, "score",   0)
    reasons = getattr(market_quality, "reasons", ())
    if label is None:
        return ""
    label_str  = _display_label(label)
    filled     = round(score / 10)
    bar        = "█" * filled + "░" * (10 - filled)
    icon       = {"ELITE": "🥇", "STRONG": "🥈", "HIGH": "🥈", "MEDIUM": "🥉", "LOW": "⚠️"}.get(label_str, "📊")
    reason_str = "  •  ".join(reasons) if reasons else "Standard market"
    return (
        f"\n\n{icon} <b>Market Quality:</b>  {label_str}  "
        f"<code>[{bar}]</code>  {score}/100"
        f"\n   <i>How reliable is the market data?  {reason_str}</i>"
    )


def _validate_final_tier(
    tier: str,
    conf: int,
    intelligence_trace: Optional[dict],
) -> str:
    """
    Compatibility helper for legacy callers.

    Tier metadata is not an alert qualification or formatting gate.  Keep the
    value unchanged so stale callers cannot silently downgrade a directional
    recommendation based on confidence or intelligence metadata.

    Parameters
    ----------
     tier               : Raw internal tier string from UDBetDecision
    conf               : Overall confidence score 0-100
    intelligence_trace : prop_intelligence trace dict (optional)
    """
    return tier


def _format_market_pressure_block(market_pressure: Optional[object]) -> str:
    """Render the Market Pressure warning section (display-only, never gates alerts)."""
    if market_pressure is None:
        return ""
    if not getattr(market_pressure, "has_pressure", False):
        return ""
    level   = getattr(market_pressure, "pressure_level", "NONE")
    reasons = getattr(market_pressure, "reasons",         ())
    icon       = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}.get(level, "⚪")
    reason_str = "  •  ".join(reasons) if reasons else ""
    text = f"\n\n{icon} <b>Market Pressure:</b>  {level}"
    if reason_str:
        text += f"\n   <i>{reason_str}</i>"
    return text


def _format_available_lines_block(
    ud_line: Optional[float],
    pp_line: Optional[float] = None,
    dk_line: Optional[float] = None,
    fd_line: Optional[float] = None,
) -> str:
    """
    Render a 📊 Available Lines block for current player-prop providers.
    DraftKings/FanDuel arguments remain accepted for legacy callers but are
    intentionally ignored so stale sportsbook rows cannot leak into current
    prop alerts.
    """
    lines = ["\n\n📊 <b>Available Line</b>" if (
        sum(v is not None for v in [pp_line, ud_line]) == 1
    ) else "\n\n📊 <b>Available Lines</b>"]

    if pp_line is not None:
        lines.append(f"\n🟣 PrizePicks:  <code>{pp_line:.1f}</code>")
    else:
        lines.append("\n🟣 PrizePicks:  <i>Unavailable for this prop</i>")
    if ud_line is not None:
        lines.append(f"\n🐶 Underdog:    <code>{ud_line:.1f}</code>")
    return "".join(lines)


def _display_label(value: object) -> str:
    """Return a human-readable enum/string label without its Python type name."""
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text.rsplit(".", 1)[-1]


def _numeric_attr(obj: Optional[object], name: str) -> Optional[float]:
    """Read a numeric display value without rendering mock/sentinel objects."""
    value = getattr(obj, name, None) if obj is not None else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _history_window_label(
    label: str,
    games: Optional[float],
    l30_games: Optional[float] = None,
) -> str:
    """Use a sample label when a so-called season value is only a short sample."""
    if label != "Season" or games is None:
        return label
    if games <= 30 and (l30_games is None or games <= l30_games):
        return f"{int(games)}-game sample"
    return label


def _format_history_block(decision: Optional[object]) -> str:
    """Render one compact hit-history block from the existing decision values."""
    if decision is None:
        return ""

    l30_games = _numeric_attr(decision, "l30_games")
    windows = (
        ("L5", "l5_hit_rate", "l5_games", "l5_over"),
        ("L10", "l10_hit_rate", "l10_games", "l10_over"),
        ("L20", "l20_hit_rate", "l20_games", "l20_over"),
        ("L30", "l30_hit_rate", "l30_games", "l30_over"),
        ("Season", "season_hit_rate", "season_games", "season_over"),
        ("H2H", "h2h_hit_rate", "h2h_games", "h2h_over"),
    )
    rows: list[str] = []
    for label, rate_name, games_name, over_name in windows:
        rate = _numeric_attr(decision, rate_name)
        games = _numeric_attr(decision, games_name)
        over = _numeric_attr(decision, over_name)
        shown_label = _history_window_label(label, games, l30_games)
        if rate is None or games is None or games <= 0:
            continue
        if over is not None:
            value = f"{rate:.0%} ({int(over)}/{int(games)})"
        else:
            value = f"{rate:.0%} ({int(games)} games)"
        rows.append(f"   <b>{shown_label}:</b> {value}")

    if not rows:
        return "\n\n📈 <b>HISTORY</b>\n   <b>L5:</b> N/A — no history available"
    return "\n\n📈 <b>HISTORY</b>\n" + "\n".join(rows)


def _format_quality_line(
    decision: Optional[object],
    score: Optional[object],
) -> str:
    """Show the existing confidence value with a plain bettor-facing label."""
    confidence = _numeric_attr(decision, "confidence")
    if confidence is None:
        confidence = _numeric_attr(score, "total")
    if confidence is None:
        confidence_display = getattr(decision, "confidence_display", None)
        if callable(confidence_display):
            try:
                confidence_display = confidence_display()
            except Exception:
                confidence_display = None
        match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*100", str(confidence_display or ""))
        if match:
            confidence = float(match.group(1))
    if confidence is None:
        return ""

    quality = int(confidence)
    stars = _bq_stars(quality)
    return f"<b>Quality:</b> <code>{quality}/100</code> {stars}"


def _format_market_quality_line(market_quality: Optional[object]) -> str:
    """Render market quality without leaking the enum's Python class name."""
    if market_quality is None:
        return ""
    label = _display_label(getattr(market_quality, "label", None))
    score = _numeric_attr(market_quality, "score")
    if not label and score is None:
        return ""
    suffix = f" · {int(score)}/100" if score is not None else ""
    return f"\n\n📊 <b>MARKET</b>\n   {label or 'Market'}{suffix}"


def _clean_reason(reason: object) -> str:
    """Remove history rows repeated in the dedicated HISTORY section."""
    text = " ".join(str(reason or "").split())
    if not text:
        return ""
    pieces = re.split(r"\s*[•·]\s*", text)
    history_prefix = re.compile(
        r"^(?:L(?:5|10|20|30)|Season(?:\s+avg\s+vs\s+line)?|H2H)\s*:",
        re.IGNORECASE,
    )
    internal_label = re.compile(r"\b(?:S|A|B|C)[- ]?tier\b", re.IGNORECASE)
    kept = [
        piece.strip()
        for piece in pieces
        if piece.strip()
        and not history_prefix.match(piece.strip())
        and not internal_label.search(piece.strip())
    ]
    # Reasons are generated internally, but are rendered as Telegram text.
    return escape(" · ".join(kept)[:160])


def _format_analyst_inline_block(
    player_name: str,
    stat_type: str,
    sport: str,
    line: float,
    score: "Optional[object]",
    decision: "Optional[object]",
    intelligence_trace: "Optional[dict]",
) -> str:
    """
    Generate a compact analyst narrative block for Telegram alerts.

    Calls engine.analyst.format_analyst_alert_block — which is a pure function
    that builds the narrative from stored decision artifacts.  Returns "" for
    non-directional/removal decisions, or when the import fails.

    Tier remains internal metadata.  The narrative is based on the actual
    confidence value and never adds a tier-adjustment message.
    """
    if decision is None:
        return ""
    rec       = getattr(decision, "recommendation", None)
    raw_tier  = getattr(decision, "decision_tier", "B") or "B"
    conf      = getattr(decision, "confidence", 0) or 0
    rlvl      = "MEDIUM"
    if score is not None:
        # Derive risk from stars: 5=LOW, 4=LOW, 3=MEDIUM, 2/1=HIGH
        stars = getattr(score, "stars", 3) or 3
        rlvl  = "LOW" if stars >= 4 else "MEDIUM" if stars >= 3 else "HIGH"

    try:
        from engine.analyst import format_analyst_alert_block
        block = format_analyst_alert_block(
            player_name        = player_name,
            stat_type          = stat_type,
            sport              = sport or "UNKNOWN",
            line               = float(line),
            decision_rec       = rec,
            decision_tier      = raw_tier,
            confidence         = int(conf),
            risk_level         = rlvl,
            intelligence_trace = intelligence_trace,
        )
        return block
    except Exception:
        return ""


def _format_intelligence_block(trace: "Optional[dict]") -> str:
    """
    Render a 🔍 Intelligence section from a prop_intelligence trace dict.

    The trace is stored in Candidate.decision_trace["prop_intelligence"] and
    contains role (label, stability, trend, summary) and matchup (label, signal,
    reasoning) data computed by engine.prop_intelligence.

    Returns an empty string when trace is None or missing the expected keys.
    """
    if not trace:
        return ""

    role    = trace.get("role",    {}) or {}
    matchup = trace.get("matchup", {}) or {}

    role_label   = (role.get("label")   or "").strip()
    role_summary = (role.get("summary") or "").strip()
    role_trend   = (role.get("trend")   or "Stable").strip()
    match_label  = (matchup.get("label")   or "").strip()
    match_rsns   = matchup.get("reasoning") or []

    # Hit rate summary from historical windows
    historical = trace.get("historical", {}) or {}
    windows    = historical.get("windows", {}) or {}
    _hr_parts: list[str] = []
    # Only actual completed-game results may be presented as hit rates.
    # Hand-authored/legacy traces without a source remain compatible, while
    # production polling-observation traces are explicitly suppressed.
    if trace.get("history_source") != "polling_observation":
        for wk, minimum in (("l5", 5), ("l10", 10), ("l20", 20), ("l30", 30)):
            w = windows.get(wk)
            if w and isinstance(w, dict) and (w.get("n") or 0) >= minimum:
                pct = round((w.get("hit_rate") or 0) * 100)
                _hr_parts.append(f"<b>{wk.upper()}:</b> {pct}%")
    _ss = historical.get("sample_strength")

    if not role_label and not match_label and not _hr_parts:
        return ""

    lines = ["\n\n🔍 <b>Intelligence</b>"]

    # Hit rates row
    if _hr_parts:
        ss_str = f"  <i>(ss:{_ss})</i>" if _ss is not None else ""
        lines.append(f"\n   📊 {' | '.join(_hr_parts)}{ss_str}")

            # Role / playtime display removed — not reliable across all sports.

    if match_label:
        match_icon = {
            "Favorable": "✅", "Neutral": "➖",
            "Tough": "⚠️",    "Difficult": "⚠️",
        }.get(match_label, "📊")
        lines.append(f"\n   {match_icon} <b>Matchup:</b>  {match_label}")
        # reasoning is stored as a string (not a list) — display as single bullet
        rsn_text = ""
        if isinstance(match_rsns, str):
            rsn_text = match_rsns.strip()
        elif isinstance(match_rsns, (list, tuple)):
            rsn_text = match_rsns[0].strip() if match_rsns else ""
        if rsn_text:
            lines.append(f"\n      <i>• {rsn_text}</i>")

    return "".join(lines)


def _market_availability_status(game_time: Optional[datetime]) -> str:
    """Return availability label based on time until game start."""
    if game_time is None:
        return "🟢 AVAILABLE NOW"
    gt = game_time.replace(tzinfo=None)
    mins_until = (gt - datetime.utcnow()).total_seconds() / 60
    if mins_until <= 0:
        return "🔴 GAME LIVE / CLOSING"
    elif mins_until <= 30:
        return "🟡 CLOSING SOON"
    else:
        return "🟢 AVAILABLE NOW"


def format_underdog_change_alert(
    player_name: str,
    team: str,
    sport: str,
    stat_type: str,
    old_line: float,
    new_line: float,
    game_time: Optional[datetime] = None,
    score: Optional[object] = None,           # UDPropScore — typed as object to avoid circular import
    validation: Optional[object] = None,      # PlayerPropValidation — typed as object
    decision: Optional[object] = None,        # UDBetDecision — typed as object
    market_quality: Optional[object] = None,  # MarketQuality — display context
    market_pressure: Optional[object] = None, # MarketPressureFlag — warning only
    pp_line: Optional[float] = None,          # PrizePicks line if available
    dk_line: Optional[float] = None,          # DraftKings line if available
    fd_line: Optional[float] = None,          # FanDuel line if available
    *,
    removed: bool = False,
    standing: bool = False,                   # True for evidence-driven alerts without line movement
    removal_reason: Optional[str] = None,     # Why prop was removed (removal alerts only)
    opponent: Optional[str] = None,           # Opponent team / player (when available)
    intelligence_trace: Optional[dict] = None, # prop_intelligence trace from decision_trace
    opening_line: Optional[float] = None,     # First ever line from PropLineHistory
    market_confirmation: Optional[dict] = None,  # OddsAPI sportsbook confirmation data
    source: str = "Underdog",
) -> str:
    """Format an alert for an Underdog prop line change or removed prop.

    Now includes an "Available Lines" block showing all 4 providers, and
    separates Market Movement Score from Bet Confidence.

    When *score* (a UDPropScore) is supplied, the grade is shown.
    When *validation* is supplied, a compact history signal line is appended.
    """
    source = source.strip() or "Underdog"
    source_upper = source.upper()
    source_emoji = "🟣" if source_upper == "PRIZEPICKS" else "🐶"
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(sport, "🎯")

    if removed:
        header         = f"🚫 <b>{source_upper} PROP REMOVED</b>"
        movement_block = f"  🐶 <b>Last Line:</b>  <code>{old_line:.1f}</code>"
    elif standing:
        header         = (
            "🐶 <b>UNDERDOG PICK</b>"
            if source_upper == "UNDERDOG"
            else "🟣 <b>PRIZEPICKS PICK</b>"
        )
        movement_block = (
            f"📊 <b>LINE</b> · {escape(source)}\n"
            f"  {source_emoji} <code>{new_line:.1f}</code>  {_line_label(new_line)}"
        )
    else:
        change         = new_line - old_line
        direction_icon = "📈" if change > 0 else "📉"
        header         = (
            "🐶 <b>UNDERDOG PICK</b>"
            if source_upper == "UNDERDOG"
            else "🟣 <b>PRIZEPICKS PICK</b>"
        )
        change_sign    = "+" if change >= 0 else ""
        movement_block = (
            f"📊 <b>LINE</b> · {escape(source)}\n"
            f"  {source_emoji} <code>{new_line:.1f}</code>  {_line_label(new_line)}\n"
            f"  Previous:         <code>{old_line:.1f}</code>\n"
            f"  {direction_icon} Move:  <code>{change_sign}{change:.1f}</code>"
        )

    opponent_str = f"\n  <b>vs:</b>      {escape(str(opponent))}" if opponent else ""
    game_str = (
        f"\n  🕐 <b>Game starts:</b>  {_format_game_time_et(game_time)}"
        if game_time else ""
    )

    # Opening line display — show only when different from current and not a removal
    opening_str = ""
    if not removed and opening_line is not None and abs(opening_line - new_line) >= 0.1:
        total_move_sign = "+" if (new_line - opening_line) >= 0 else ""
        opening_str = (
            f"\n  <b>Opened:</b>  <code>{opening_line:.1f}</code>"
            f"  <i>(total move: {total_move_sign}{new_line - opening_line:.1f})</i>"
        )

    _thick = "━" * 18

    # ── Compact alert (decision-first) ──────────────────────────────────────
    change = (new_line - old_line) if not removed else 0.0
    change_sign = "+" if change >= 0 else ""

    # Pick / quality from decision.  Tier remains internal metadata only.
    rec = getattr(decision, "recommendation", None) if decision else None
    conf = getattr(decision, "confidence", None) if decision else None
    if conf is None and score is not None:
        conf = getattr(score, "total", None)

    pick_line = ""
    strong_under_line = ""
    if rec in ("OVER", "UNDER") and not removed:
        pick_emoji = "🟢" if rec == "OVER" else "🔴"
        pick_line = f"📌 <b>PICK:</b>  {pick_emoji} <b>{rec} {new_line:.1f}</b>"
        if conf is not None:
            _bq_int = int(conf)
            # _bq_stars remains the source of truth for the displayed quality stars.
            pick_line += f"\n{_format_quality_line(decision, score)}"
            # Telegram has one user-facing classification.  Retain the
            # positive quality cue, but never append ACTIONABLE or directional
            # signal labels beside the canonical PICK.
            if _bq_int >= 80:
                pick_line += "\n💪 STRONG BET"
        _is_tier1_sport = sport.upper() not in {"MLB", "NFL"}

    # Market quality one-liner
    mq_line = _format_market_quality_line(market_quality) if not removed else ""
    avail_block = _format_available_lines_block(
        ud_line=new_line if not removed else old_line,
        pp_line=pp_line,
        dk_line=dk_line,
        fd_line=fd_line,
    ) if not removed else ""

    if removed:
        body_lines = [
            _thick,
            header,
            _thick,
            "",
            f"{sport_icon} <b>{escape(str(sport))} — {escape(str(stat_type))}</b>",
            f"👤 <b>{escape(str(player_name))}</b>",
            "",
            f"{source_emoji} {escape(source)} Last line: <code>{old_line:.1f}</code>",
            f"Removal: {escape(removal_reason or 'Market no longer available')}",
            "",
            f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
            "",
            _thick,
        ]
    else:
        # OddsAPI sportsbook confirmation footer (S/A tier only)
        _confirm_line = ""
        if market_confirmation is not None:
            _cn = market_confirmation.get("notes", "")
            if _cn:
                _confirm_line = f"📡 <b>Market Check:</b>  {escape(str(_cn))}"

        body_lines = [
            _thick,
            header,
            _thick,
            "",
            f"{sport_icon} <b>{escape(str(sport))} — {escape(str(stat_type))}</b>",
            f"👤 <b>{escape(str(player_name))}</b>",
            "",
            movement_block,
            opponent_str,
            game_str,
            opening_str,
            avail_block.lstrip("\n"),
            "",
            pick_line,
        ]
        if decision is not None:
            decision_reason = _clean_reason(getattr(decision, "reason", ""))
            history = _format_history_block(decision)
            if history:
                body_lines += [history]
            if decision_reason:
                body_lines += [f"\n💬 <b>Reason:</b>\n   <i>{decision_reason}</i>"]
        if mq_line:
            body_lines += [mq_line]
        if _confirm_line:
            body_lines += [_confirm_line]
        body_lines += [
            "",
            f"📍 <b>Status:</b>  {_market_availability_status(game_time)}",
            f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
            "",
            _thick,
        ]

    # Drop empty strings from optional lines
    parts = [ln for ln in body_lines if ln is not None and ln != ""]
    return "\n".join(parts)


def format_market_move_detected(
    player_name: str,
    team: str,
    sport: str,
    stat_type: str,
    old_line: float,
    new_line: float,
    game_time: Optional[datetime] = None,
) -> str:
    """Lightweight alert for significant line moves that do not yet qualify as bet picks.

    Fires when Underdog moves a line by ≥ MIN_UNDERDOG_LINE_CHANGE but the prop
    has not passed the full scoring + confidence + tier gate.  Keeps the user
    informed of sharp market movement without implying a betting edge.
    """
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(sport, "🎯")

    change = new_line - old_line
    change_sign = "+" if change >= 0 else ""
    direction_icon = "📈" if change > 0 else "📉"

    game_str = (
        f"\n🕐 <b>Game:</b>  {game_time.strftime('%b %d %H:%M')} UTC"
        if game_time else ""
    )

    _thick = "━" * 18
    body_lines = [
        _thick,
        f"{direction_icon} <b>MARKET MOVE DETECTED</b>",
        _thick,
        "",
        f"{sport_icon} <b>{sport} — {stat_type}</b>",
        f"👤 <b>{player_name}</b>",
        "",
        f"🐶 <b>Line:</b>  <code>{old_line:.1f}</code>  →  <code>{new_line:.1f}</code>",
        f"📉 <b>Movement:</b>  <code>{change_sign}{change:.1f}</code>",
        "",
        "📊 <b>Status:</b>  Tracking — evaluating for bet quality",
        "<i>This is a market signal, not yet a bet pick.</i>",
        game_str,
        "",
        f"🕐 <b>Line as of:</b> "
        f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
        "",
        _thick,
    ]
    parts = [ln for ln in body_lines if ln is not None]
    return "\n".join(parts)


# ── Underdog new-prop alert ────────────────────────────────────────────────────

def format_underdog_new_prop_alert(
    player_name: str,
    team: str,
    sport: str,
    stat_type: str,
    line_value: float,
    game_time: Optional[datetime] = None,
    score: Optional[object] = None,           # UDPropScore
    validation: Optional[object] = None,      # PlayerPropValidation
    decision: Optional[object] = None,        # UDBetDecision
    market_quality: Optional[object] = None,  # MarketQuality — display context
    market_pressure: Optional[object] = None, # MarketPressureFlag — warning only
    pp_line: Optional[float] = None,          # PrizePicks line if available
    dk_line: Optional[float] = None,          # DraftKings line if available
    fd_line: Optional[float] = None,          # FanDuel line if available
    *,
    low_line_threshold: float = 1.0,
    opponent: Optional[str] = None,           # Opponent team / player (when available)
    intelligence_trace: Optional[dict] = None, # prop_intelligence trace from decision_trace
    market_confirmation: Optional[dict] = None,  # OddsAPI sportsbook confirmation data
    source: str = "Underdog",
) -> str:
    """Format a 🚨 UNDERDOG PROP LIVE alert for a first-appearance prop.

    Fires when a (player_name, stat_type) pair is seen for the very first
    time AND has sufficient history to justify an immediate alert.
    The validation gate ensures props with no history go to digest only.
    """
    source = source.strip() or "Underdog"
    source_upper = source.upper()
    source_emoji = "🟣" if source_upper == "PRIZEPICKS" else "🐶"
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(sport, "🎯")

    opponent_str = f"  <b>vs:</b> {escape(str(opponent))}" if opponent else ""
    game_str = (
        f"  🕐 <b>Game starts:</b> {_format_game_time_et(game_time)}"
        if game_time else ""
    )

    # Quality labels are informational only; direction is the sole
    # qualification gate.
    # _bq_stars is applied inside _format_quality_line so quality stars still
    # come from decision confidence rather than the raw market score.
    # Reason bullets
    reasons = ["Qualified current-line player evidence"]
    if line_value <= low_line_threshold:
        reasons.append(f"Low starting line ({line_value} ≤ {low_line_threshold})")
    if score is not None and getattr(score, "stars", 0) >= 3:
        reasons.append("Score qualifies for current market review")
    if (
        validation is not None
        and getattr(validation, "has_supporting_data", False)
        and str(getattr(validation, "data_source", "")).lower() == "real"
    ):
        reasons.append(
            f"Supporting game history available ({getattr(validation, 'n_real_games', 0)} games)"
        )

    avail_block = _format_available_lines_block(
        ud_line=line_value,
        pp_line=pp_line,
        dk_line=dk_line,
        fd_line=fd_line,
    )
    quality_line = _format_quality_line(decision, score)
    history_block = _format_history_block(decision)
    market_line = _format_market_quality_line(market_quality)
    pressure_block = _format_market_pressure_block(market_pressure)
    context_line = (
        f"  <b>First Seen:</b>    {datetime.utcnow().strftime('%H:%M UTC')}"
        + (f"\n{opponent_str}" if opponent_str else "")
        + (f"\n{game_str}" if game_str else "")
        + (f"\n{quality_line}" if quality_line else "")
        + (f"\n{history_block}" if history_block else "")
        + (f"\n{market_line}" if market_line else "")
        + (f"\n{pressure_block}" if pressure_block else "")
    )

    _thick = "━" * 18
    parts = [
        _thick,
        (
            "🐶 <b>UNDERDOG PICK</b>"
            if source_upper == "UNDERDOG"
            else "🟣 <b>PRIZEPICKS PICK</b>"
        ),
        _thick,
        "",
            f"{sport_icon} <b>{escape(str(sport))} — {escape(str(stat_type))}</b>",
            f"👤 <b>{escape(str(player_name))}</b>",
        "",
        _div(),
        avail_block.lstrip("\n"),
        "",
        _div(),
        f"  {source_emoji} <b>LINE · {escape(source)}:</b>  <code>{line_value:.1f}</code>  {_line_label(line_value)}",
        context_line,
        "",
        "<b>Reason:</b>\n" + " · ".join(escape(reason) for reason in reasons),
        *([f"📡 <b>Market Check:</b>  {escape(str(market_confirmation['notes']))}"]
          if market_confirmation and market_confirmation.get("notes") else []),
        "",
        f"{EMOJI['clock']} <b>Line as of:</b> "
        f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
        "",
        _thick,
    ]
    return "\n".join(parts)


# ── Underdog end-of-cycle new-prop digest ─────────────────────────────────────

def format_underdog_new_prop_cycle_summary(
    new_props: "list[dict]",
    *,
    max_shown: int = 8,
) -> str:
    """Format a 📋 end-of-cycle digest for all new Underdog props detected this cycle.

    Each entry in ``new_props`` is a dict with keys:
      player, stat_type, sport, team, line, score (UDPropScore|None),
      immediate (bool), game_time (datetime|None).

    Sorted display order: 0.5 lines first → line asc → stars desc.
       Immediately-alerted props are marked with ⚡; other entries show their
       quality stars without exposing internal classification names.
    """
    if not new_props:
        return ""

    total       = len(new_props)
    n_immediate = sum(1 for p in new_props if p.get("immediate"))
    n_half_line = sum(1 for p in new_props if (p.get("line") or 99.0) <= 0.5)

    def _sort_key(p: dict) -> tuple:
        stars = getattr(p.get("score"), "stars", 0)
        line  = p.get("line") or 99.0
        # 0.5 lines first, then ascending line, then descending stars
        return (0 if line <= 0.5 else 1, line, -stars)

    sorted_props = sorted(new_props, key=_sort_key)
    shown        = sorted_props[:max_shown]
    overflow     = total - len(shown)

    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }

    parts = [
        f"📋 <b>UNDERDOG NEW PROPS</b>  —  <b>{total}</b> detected",
        "",
        _div(),
        f"  ⚡ Immediate alerts:  <b>{n_immediate}</b>",
        f"  🎯 0.5 lines found:   <b>{n_half_line}</b>",
        "",
        "<b>Top Opportunities</b>",
    ]

    for p in shown:
        score  = p.get("score")
        stars  = getattr(score, "stars_display", "·····") if score else "·····"
        sport  = p.get("sport", "")
        icon   = sport_icon.get(sport, "🎯")
        line   = p.get("line") or 0.0
        marker = "⚡" if p.get("immediate") else "•"
        parts.append(
            f"  {marker}  {icon} {p['player']} — {p['stat_type']}"
            f"  @{line}  {stars}"
        )

    if overflow > 0:
        parts.append(f"  <i>...and {overflow} more</i>")

    parts += [
        "",
        f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(parts)


# ── /market command response formatter ────────────────────────────────────────

def format_consensus_summary(results: list[ConsensusResult]) -> str:
    """Format a summary of consensus results for the /market command."""
    if not results:
        return (
            f"📊 <b>Market Consensus</b>\n\n"
            f"No cross-book consensus data available.\n"
            f"<i>Data accumulates as connectors poll live odds.</i>"
        )

    ineffs = sum(1 for r in results if r.has_inefficiency)
    total  = len(results)

    parts = [
        f"📊 <b>Market Consensus Summary</b>",
        "",
        f"Markets tracked:      <code>{total}</code>",
        f"With inefficiencies:  <code>{ineffs}</code>",
        "",
        _div(),
    ]

    # Show top markets with inefficiencies first
    sorted_results = sorted(results, key=lambda r: len(r.outliers), reverse=True)
    for cr in sorted_results[:8]:
        ineff_flag = "⚠️" if cr.has_inefficiency else "✅"
        consensus_str = format_odds(cr.consensus_odds)
        parts.append(
            f"{ineff_flag} <b>{cr.selection}</b>\n"
            f"   {cr.event} | {cr.market_type}\n"
            f"   Consensus: <code>{consensus_str}</code> ({cr.book_count} books)"
        )
        if cr.has_inefficiency:
            for ineff in cr.outliers:
                sign = "+" if ineff.deviation > 0 else ""
                parts.append(
                    f"   📌 {ineff.sportsbook}: <code>{format_odds(ineff.offered_odds)}</code>"
                    f" [{sign}{ineff.deviation}]"
                )
        parts.append("")

    return "\n".join(parts)


# ── /clv command response formatter ───────────────────────────────────────────

def format_clv_history(records: list) -> str:
    """Format CLV history records for the /clv command."""
    if not records:
        return (
            f"💎 <b>CLV Performance</b>\n\n"
            f"No CLV records yet.\n"
            f"<i>CLV is computed when alerted markets close.</i>"
        )

    total   = len(records)
    beaten  = sum(1 for r in records if r.clv_pct > 0)
    avg_clv = sum(r.clv_pct for r in records) / total if total > 0 else 0
    sign    = "+" if avg_clv >= 0 else ""

    parts = [
        f"💎 <b>CLV Performance</b>",
        "",
        f"Records:     <code>{total}</code>",
        f"Beat close:  <code>{beaten}/{total}</code>  ({beaten/total*100:.0f}%)",
        f"Avg CLV:     <code>{sign}{avg_clv:.2f}%</code>",
        "",
        _div(),
    ]

    for r in records[:10]:
        bet_sign = "+" if r.clv_pct >= 0 else ""
        emoji = "✅" if r.clv_pct > 0 else "❌"
        parts.append(
            f"{emoji} <b>{r.selection}</b>\n"
            f"   CLV: <code>{bet_sign}{r.clv_pct:.2f}%</code>  "
            f"bet={format_odds(r.bet_odds)} close={format_odds(r.closing_odds)}\n"
            f"   {r.computed_at.strftime('%b %d %H:%M')} UTC"
        )

    return "\n".join(parts)
