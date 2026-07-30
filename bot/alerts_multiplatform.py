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
from typing import Optional

from alerts import EMOJI, _div, _risk_section, format_odds, format_probability, RiskFactor
from engine.consensus import ConsensusResult, MarketInefficiency
from engine.clv import CLVOpportunity, CLVResult


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

    dir_icon  = EMOJI["down"] if steam_direction == "DOWN" else EMOJI["up"]
    dir_label = "FALLING" if steam_direction == "DOWN" else "RISING"
    books_str = ", ".join(books_moved) if books_moved else "—"
    sharp_str = ", ".join(sharp_books) if sharp_books else "None detected"
    change    = current_odds - opening_odds
    filled    = round(steam_score / 10)
    score_bar = "█" * filled + "░" * (10 - filled)

    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊",
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
        "NHL": "🏒", "UFC": "🥊",
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
        *_risk_section(risk_factors),
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
        "NHL": "🏒", "UFC": "🥊",
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
        *_risk_section(risk_factors),
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

def format_underdog_change_alert(
    player_name: str,
    team: str,
    sport: str,
    stat_type: str,
    old_line: float,
    new_line: float,
    game_time: Optional[datetime] = None,
    score: Optional[object] = None,   # UDPropScore — typed as object to avoid circular import
    *,
    removed: bool = False,
) -> str:
    """Format an alert for an Underdog prop line change or removed prop.

    When *score* (a UDPropScore) is supplied, a grade line is appended that
    shows the tier (S/A/B), star rating (★★★☆☆), and raw score total.
    """
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊",
    }.get(sport, "🎯")

    if removed:
        header = "🚫 <b>UNDERDOG PROP REMOVED</b>"
        change_line = f"  <b>Last Line:</b>  {old_line}"
        direction   = ""
    else:
        change      = new_line - old_line
        direction   = "📈 HIGHER" if change > 0 else "📉 LOWER"
        header      = f"🔄 <b>UNDERDOG LINE CHANGE</b>  {direction}"
        change_sign = "+" if change >= 0 else ""
        change_line = (
            f"  <b>Old Line:</b>  {old_line}\n"
            f"  <b>New Line:</b>  {new_line}\n"
            f"  <b>Change:</b>    <code>{change_sign}{change:.1f}</code>"
        )

    game_str = f"\n  <b>Game:</b>    {game_time.strftime('%b %d %H:%M')} UTC" if game_time else ""

    # Grade block — only shown when a UDPropScore was computed
    grade_str = ""
    if score is not None:
        # Access tier/stars/total/stars_display via attribute lookup so this
        # module does not need to import engine.ud_scoring directly.
        tier   = getattr(score, "tier",          "?")
        stars  = getattr(score, "stars",          0)
        total  = getattr(score, "total",          0)
        s_disp = getattr(score, "stars_display",  "?" * 5)
        n_hist = getattr(score, "n_history",      0)
        grade_str = (
            f"\n\n📊 <b>Grade:</b>  <code>{tier}</code>  {s_disp}  "
            f"<code>{total}/100</code>"
            f"  <i>(n={n_hist})</i>"
        )

    parts = [
        header,
        "",
        f"{sport_icon} <b>{sport} — {stat_type}</b>",
        f"👤 <b>{player_name}</b>",
        "",
        _div(),
        change_line + game_str + grade_str,
        "",
        f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(parts)


# ── Underdog new-prop alert ────────────────────────────────────────────────────

def format_underdog_new_prop_alert(
    player_name: str,
    team: str,
    sport: str,
    stat_type: str,
    line_value: float,
    game_time: Optional[datetime] = None,
    score: Optional[object] = None,    # UDPropScore
    *,
    low_line_threshold: float = 1.0,
) -> str:
    """Format a 🚨 UNDERDOG PROP LIVE alert for a first-appearance prop.

    Fires when a (player_name, stat_type) pair is seen for the very first
    time in the Underdog feed.  Score may be present even with no history
    (n_history=0) if the scoring model ran with defaults.
    """
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊",
    }.get(sport, "🎯")

    game_str = (
        f"\n  <b>Game:</b>        {game_time.strftime('%b %d %H:%M')} UTC"
        if game_time else ""
    )

    # Grade block — shown even when n_history=0 so tier/stars are visible
    grade_str = ""
    if score is not None:
        tier   = getattr(score, "tier",         "?")
        stars  = getattr(score, "stars",         0)
        total  = getattr(score, "total",         0)
        s_disp = getattr(score, "stars_display", "?" * 5)
        n_hist = getattr(score, "n_history",     0)
        grade_str = (
            f"\n\n📊 <b>Grade:</b>  <code>{tier}</code>  {s_disp}  "
            f"<code>{total}/100</code>"
            f"  <i>(n={n_hist})</i>"
        )

    # Reason bullets
    reasons = ["• New prop detected"]
    if line_value <= low_line_threshold:
        reasons.append(f"• Low starting line ({line_value} ≤ {low_line_threshold})")
    if score is not None and getattr(score, "stars", 0) >= 3:
        tier = getattr(score, "tier", "?")
        reasons.append(f"• Score qualifies ({tier}-tier)")

    parts = [
        "🚨 <b>UNDERDOG PROP LIVE</b>",
        "",
        f"{sport_icon} <b>{sport} — {stat_type}</b>",
        f"👤 <b>{player_name}</b>",
        "",
        _div(),
        f"  <b>Starting Line:</b>  {line_value}",
        f"  <b>First Seen:</b>    {datetime.utcnow().strftime('%H:%M UTC')}"
        + game_str
        + grade_str,
        "",
        "<b>Reason:</b>",
        "\n".join(reasons),
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
