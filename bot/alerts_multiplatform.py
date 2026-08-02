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


def _line_label(line: float) -> str:
    """General line-level reference label — context only, not a difficulty rating."""
    if line <= 0.5:
        return "🟢 Low Line / Goblin Discount"
    elif line <= 1.5:
        return "⚪ Standard Line"
    else:
        return "🔴 Higher Difficulty Line"

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

def _format_decision_block(decision: Optional[object]) -> str:
    """
    Format the full OVER / UNDER / PASS evaluation block for Underdog alerts.

    Shows tier, confidence, all per-window hit-rate statistics, and a reason.
    Returns an empty string when *decision* is None.
    """
    if decision is None:
        return ""

    rec       = getattr(decision, "recommendation", "PASS")
    emoji     = getattr(decision, "recommendation_emoji", lambda: "⚪")()
    tier_disp = getattr(decision, "tier_display",         lambda: "—")()
    conf      = getattr(decision, "confidence_display",   lambda: "—")()
    reason    = getattr(decision, "reason", "")
    avg_disp  = getattr(decision, "avg_vs_line_display",  lambda: "N/A")()

    win_fn = getattr(decision, "window_display", None)

    def _w(g, o, u, r, a) -> str:
        """Render one evidence row."""
        if win_fn and callable(win_fn):
            return win_fn(g, o, u, r, a)
        if g is None or r is None:
            return "N/A"
        rate_str = f"{r:.0%}"
        avg_str  = f"  avg {a:.1f}" if a is not None else ""
        return f"{o}/{g} ({rate_str}){avg_str}"

    def _a(name):
        return getattr(decision, name, None)

    is_pick = rec != "PASS"

    tier_line = f"\n   {tier_disp}" if is_pick else ""
    conf_line = f"\n   <b>Confidence:</b>    {conf}" if is_pick else ""

    if is_pick:
        evidence_block = (
            f"\n\n📊 <b>Evidence:</b>"
            f"\n   <b>L5:</b>          {_w(_a('l5_games'),     _a('l5_over'),     _a('l5_under'),     _a('l5_hit_rate'),     _a('l5_avg'))}"
            f"\n   <b>L10:</b>         {_w(_a('l10_games'),    _a('l10_over'),    _a('l10_under'),    _a('l10_hit_rate'),    _a('l10_avg'))}"
            f"\n   <b>L20:</b>         {_w(_a('l20_games'),    _a('l20_over'),    _a('l20_under'),    _a('l20_hit_rate'),    _a('l20_avg'))}"
            f"\n   <b>L30:</b>         {_w(_a('l30_games'),    _a('l30_over'),    _a('l30_under'),    _a('l30_hit_rate'),    _a('l30_avg'))}"
            f"\n   <b>Season:</b>      {_w(_a('season_games'), _a('season_over'), _a('season_under'), _a('season_hit_rate'), _a('season_avg'))}"
            f"\n   <b>H2H:</b>         {_w(_a('h2h_games'),    _a('h2h_over'),    _a('h2h_under'),    _a('h2h_hit_rate'),    _a('h2h_avg'))}"
            f"\n   <b>Avg vs line:</b> {avg_disp}"
        )
    else:
        evidence_block = ""

    reason_block = f"\n\n💬 <b>Reason:</b>\n   <i>{reason}</i>" if reason else ""

    return (
        f"\n\n{_div()}"
        f"\n🎯 <b>Recommendation:</b>  {emoji} {rec}"
        f"{tier_line}"
        f"{conf_line}"
        f"{evidence_block}"
        f"{reason_block}"
    )


def _format_market_quality_block(market_quality: Optional[object]) -> str:
    """Render the Market Quality section for Underdog alerts (informational)."""
    if market_quality is None:
        return ""
    label   = getattr(market_quality, "label",   None)
    score   = getattr(market_quality, "score",   0)
    reasons = getattr(market_quality, "reasons", ())
    if label is None:
        return ""
    label_str  = label.value if hasattr(label, "value") else str(label)
    filled     = round(score / 10)
    bar        = "█" * filled + "░" * (10 - filled)
    icon       = {"ELITE": "🥇", "HIGH": "🥈", "MEDIUM": "🥉", "LOW": "⚠️"}.get(label_str, "📊")
    reason_str = "  •  ".join(reasons) if reasons else "Standard market"
    return (
        f"\n\n{icon} <b>Market Quality:</b>  {label_str}  "
        f"<code>[{bar}]</code>  {score}/100"
        f"\n   <i>{reason_str}</i>"
    )


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
    Render a 📊 Available Lines block showing only providers with real data.
    Providers with no current data are omitted entirely.
    """
    lines = ["\n\n📊 <b>Available Line</b>" if (
        sum(v is not None for v in [pp_line, ud_line, dk_line, fd_line]) == 1
    ) else "\n\n📊 <b>Available Lines</b>"]

    if pp_line is not None:
        lines.append(f"\n🟣 PrizePicks:  <code>{pp_line:.1f}</code>")
    if ud_line is not None:
        lines.append(f"\n🐶 Underdog:    <code>{ud_line:.1f}</code>")
    if dk_line is not None:
        lines.append(f"\n🎰 DraftKings:  <code>{dk_line:.1f}</code>")
    if fd_line is not None:
        lines.append(f"\n🦊 FanDuel:     <code>{fd_line:.1f}</code>")

    return "".join(lines)


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
    PASS/removal decisions, or when the import fails.
    """
    if decision is None:
        return ""
    rec   = getattr(decision, "recommendation", None)
    tier  = getattr(decision, "decision_tier", "B") or "B"
    conf  = getattr(decision, "confidence", 0) or 0
    rlvl  = "MEDIUM"
    if score is not None:
        # Derive risk from stars: 5=LOW, 4=LOW, 3=MEDIUM, 2/1=HIGH
        stars = getattr(score, "stars", 3) or 3
        rlvl  = "LOW" if stars >= 4 else "MEDIUM" if stars >= 3 else "HIGH"
    try:
        from engine.analyst import format_analyst_alert_block
        return format_analyst_alert_block(
            player_name        = player_name,
            stat_type          = stat_type,
            sport              = sport or "UNKNOWN",
            line               = float(line),
            decision_rec       = rec,
            decision_tier      = tier,
            confidence         = int(conf),
            risk_level         = rlvl,
            intelligence_trace = intelligence_trace,
        )
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
    for wk in ("l5", "l10", "l20"):
        w = windows.get(wk)
        if w and isinstance(w, dict) and (w.get("n") or 0) >= 3:
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

    if role_label:
        role_icon = {"Starter": "🟢", "Reserve": "🟡", "Bench": "🔴"}.get(role_label, "⚪")
        lines.append(f"\n   {role_icon} <b>Role:</b>  {role_label}")
        if role_summary:
            lines.append(f"  ·  <i>{role_summary}</i>")
        if role_trend and role_trend not in ("Stable", ""):
            lines.append(f"\n   📈 <b>Trend:</b>  {role_trend}")

    if match_label:
        match_icon = {
            "Favorable": "✅", "Neutral": "➖",
            "Tough": "⚠️",    "Difficult": "⚠️",
        }.get(match_label, "📊")
        lines.append(f"\n   {match_icon} <b>Matchup:</b>  {match_label}")
        for rsn in list(match_rsns)[:3]:   # up to 3 reasoning bullets
            if rsn:
                lines.append(f"\n      <i>• {rsn}</i>")

    return "".join(lines)


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
) -> str:
    """Format an alert for an Underdog prop line change or removed prop.

    Now includes an "Available Lines" block showing all 4 providers, and
    separates Market Movement Score from Bet Confidence.

    When *score* (a UDPropScore) is supplied, the grade is shown.
    When *validation* is supplied, a compact history signal line is appended.
    """
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(sport, "🎯")

    if removed:
        header         = "🚫 <b>UNDERDOG PROP REMOVED</b>"
        movement_block = f"  🐶 <b>Last Line:</b>  <code>{old_line:.1f}</code>"
    elif standing:
        header         = "🎯 <b>UNDERDOG STANDING PLAY</b>"
        movement_block = (
            f"📊 <b>Underdog Line</b>\n"
            f"  🐶 <code>{new_line:.1f}</code>  {_line_label(new_line)}"
        )
    else:
        change         = new_line - old_line
        direction_icon = "📈" if change > 0 else "📉"
        header         = f"{direction_icon} <b>UNDERDOG LINE MOVE</b>"
        change_sign    = "+" if change >= 0 else ""
        movement_block = (
            f"📊 <b>Underdog Line</b>\n"
            f"  🐶 <code>{new_line:.1f}</code>  {_line_label(new_line)}\n"
            f"  Previous:         <code>{old_line:.1f}</code>\n"
            f"  Movement:         <code>{change_sign}{change:.1f}</code>"
        )

    opponent_str = f"\n  <b>vs:</b>      {opponent}" if opponent else ""
    game_str = f"\n  <b>Game:</b>    {game_time.strftime('%b %d %H:%M')} UTC" if game_time else ""

    # Opening line display — show only when different from current and not a removal
    opening_str = ""
    if not removed and opening_line is not None and abs(opening_line - new_line) >= 0.1:
        total_move_sign = "+" if (new_line - opening_line) >= 0 else ""
        opening_str = (
            f"\n  <b>Opened:</b>  <code>{opening_line:.1f}</code>"
            f"  <i>(total move: {total_move_sign}{new_line - opening_line:.1f})</i>"
        )

    # Grade + movement score vs bet confidence separation
    grade_str = ""
    if score is not None:
        tier      = getattr(score, "tier",          "?")
        s_disp    = getattr(score, "stars_display",  "?" * 5)
        total     = getattr(score, "total",          0)
        n_hist    = getattr(score, "n_history",      0)
        move_vel  = getattr(score, "move_velocity",  None)  # market movement component

        grade_str = f"\n\n📊 <b>Grade:</b>  <code>{tier}</code>  {s_disp}  <code>{total}/100</code>  <i>(n={n_hist})</i>"

        if not removed and move_vel is not None:
            grade_str += (
                f"\n\n📉 <b>Market Movement Score:</b>  <code>{move_vel}/100</code>"
                f"  <i>(how significant is the line move)</i>"
                f"\n🎯 <b>Bet Confidence:</b>         <code>{total}/100</code>"
                f"  <i>(composite: history + movement)</i>"
                f"\n<i>Market activity ≠ betting edge.</i>"
            )

    # Validation block
    validation_str = ""
    if validation is not None and getattr(validation, "has_supporting_data", False):
        validation_str = (
            f"\n💡 <b>History:</b>  "
            f"<code>{getattr(validation, 'rate_summary', lambda: '')()}</code>"
        )

    # Available lines block (all 4 providers)
    avail_block = _format_available_lines_block(
        ud_line = new_line if not removed else old_line,
        pp_line = pp_line,
        dk_line = dk_line,
        fd_line = fd_line,
    )

    _thick = "━" * 18
    _removal_reason_str = ""
    if removed:
        _reason = removal_reason or "Market no longer available from provider"
        _removal_reason_str = f"\n  <b>Removal Reason:</b>  {_reason}"

    parts = [
        _thick,
        header,
        _thick,
        "",
        f"{sport_icon} <b>{sport} — {stat_type}</b>",
        f"👤 <b>{player_name}</b>",
        "",
        _div(),
        avail_block.lstrip("\n"),
        "",
        _div(),
        movement_block + _removal_reason_str + opening_str + opponent_str + game_str
        + grade_str + validation_str
        + _format_decision_block(decision)
        + _format_market_quality_block(market_quality)
        + _format_market_pressure_block(market_pressure)
        + _format_intelligence_block(intelligence_trace)
        + ("" if removed else _format_analyst_inline_block(
            player_name        = player_name,
            stat_type          = stat_type,
            sport              = sport or "UNKNOWN",
            line               = new_line,
            score              = score,
            decision           = decision,
            intelligence_trace = intelligence_trace,
        )),
        "",
        f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
        "",
        _thick,
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
) -> str:
    """Format a 🚨 UNDERDOG PROP LIVE alert for a first-appearance prop.

    Fires when a (player_name, stat_type) pair is seen for the very first
    time AND has sufficient history to justify an immediate alert.
    The validation gate ensures props with no history go to digest only.
    """
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(sport, "🎯")

    opponent_str = f"\n  <b>vs:</b>          {opponent}" if opponent else ""
    game_str = (
        f"\n  <b>Game:</b>        {game_time.strftime('%b %d %H:%M')} UTC"
        if game_time else ""
    )

    # Grade block
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

    # Validation block — only show when supporting history is available
    validation_str = ""
    if validation is not None and getattr(validation, "has_supporting_data", False):
        validation_str = (
            f"\n💡 <b>History:</b>  "
            f"<code>{getattr(validation, 'rate_summary', lambda: '')()}</code>"
        )

    # Reason bullets
    reasons = ["• New prop detected"]
    if line_value <= low_line_threshold:
        reasons.append(f"• Low starting line ({line_value} ≤ {low_line_threshold})")
    if score is not None and getattr(score, "stars", 0) >= 3:
        tier = getattr(score, "tier", "?")
        reasons.append(f"• Score qualifies ({tier}-tier)")
    if validation is not None and getattr(validation, "has_supporting_data", False):
        reasons.append(f"• Supporting history available ({getattr(validation, 'n_history', 0)} snapshots)")

    # Available lines block (all 4 providers)
    avail_block = _format_available_lines_block(
        ud_line = line_value,
        pp_line = pp_line,
        dk_line = dk_line,
        fd_line = fd_line,
    )

    _thick = "━" * 18
    parts = [
        _thick,
        "🚨 <b>UNDERDOG PROP LIVE</b>",
        _thick,
        "",
        f"{sport_icon} <b>{sport} — {stat_type}</b>",
        f"👤 <b>{player_name}</b>",
        "",
        _div(),
        avail_block.lstrip("\n"),
        "",
        _div(),
        f"  🐶 <b>Underdog Line:</b>  <code>{line_value:.1f}</code>  {_line_label(line_value)}",
        f"  <b>First Seen:</b>    {datetime.utcnow().strftime('%H:%M UTC')}"
        + opponent_str
        + game_str
        + grade_str
        + validation_str
        + _format_decision_block(decision)
        + _format_market_quality_block(market_quality)
        + _format_market_pressure_block(market_pressure)
        + _format_intelligence_block(intelligence_trace)
        + _format_analyst_inline_block(
            player_name        = player_name,
            stat_type          = stat_type,
            sport              = sport or "UNKNOWN",
            line               = line_value,
            score              = score,
            decision           = decision,
            intelligence_trace = intelligence_trace,
        ),
        "",
        "<b>Reason:</b>",
        "\n".join(reasons),
        "",
        f"{EMOJI['clock']} <i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>",
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
    Immediately-alerted props are marked with ⚡; others with a tier badge.
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
        tier   = getattr(score, "tier",          "?")     if score else "?"
        sport  = p.get("sport", "")
        icon   = sport_icon.get(sport, "🎯")
        line   = p.get("line") or 0.0
        marker = "⚡" if p.get("immediate") else f"<code>{tier}</code>"
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
