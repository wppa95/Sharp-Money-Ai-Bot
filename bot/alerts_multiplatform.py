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

from datetime import datetime, timezone
from html import escape
import math
import re
from typing import Optional


def _format_game_time_et(dt: datetime) -> str:
    """Convert a naive-UTC datetime to a readable date/time in ET.

    Returns e.g. ``"Sep 22, 8:40 PM ET"`` for Telegram display.
    Falls back to a UTC date/time if the display-zone conversion fails.
    UTC is kept internally everywhere; this is display-only.
    """
    try:
        from zoneinfo import ZoneInfo
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            dt_et = dt.astimezone(ZoneInfo("America/New_York"))
        else:
            dt_et = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(
                ZoneInfo("America/New_York")
            )
        # %-I strips the leading zero on Linux ("8:40 PM ET" not "08:40 PM ET")
        return dt_et.strftime("%b %-d, %-I:%M %p ET")
    except Exception:
        try:
            dt_utc = (
                dt.astimezone(timezone.utc)
                if dt.tzinfo is not None and dt.utcoffset() is not None
                else dt
            )
            return dt_utc.strftime("%b %-d, %-I:%M %p UTC")
        except Exception:
            return "Time unavailable"


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
from engine.ud_bet_decision import evidence_source_label

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
     Format the full OVER / UNDER evaluation block for internal/legacy callers.

     Shows all per-window history statistics and a reason. Quality metadata is
     retained only in the internal decision object.
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
        f"{_format_history_block(decision)}"
        f"{reason_block}"
    )


def _format_market_quality_block(market_quality: Optional[object]) -> str:
    """Compatibility stub; market quality is never rendered in Telegram."""
    return ""


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
    *,
    provider: Optional[str] = None,
) -> str:
    """
    Render a 📊 Available Lines block for current player-prop providers.

    Provider alerts are intentionally isolated: when ``provider`` is
    Underdog or PrizePicks, only that provider's line is rendered.  Calls
    without a provider retain the legacy comparison block for non-delivery
    callers.

    DraftKings/FanDuel arguments remain accepted for legacy callers but are
    intentionally ignored so stale sportsbook rows cannot leak into current
    prop alerts.
    """
    provider_upper = str(provider or "").strip().upper()
    if provider_upper == "UNDERDOG":
        display_lines = [("🎯", "Player Prop", ud_line)]
    elif provider_upper == "PRIZEPICKS":
        display_lines = [("🎯", "Player Prop", pp_line)]
    else:
        display_lines = [
            ("🎯", "Player Prop", pp_line),
            ("🎯", "Player Prop", ud_line),
        ]

    available_count = sum(value is not None for _, _, value in display_lines)
    lines = [
        "\n\n📊 <b>Available Line</b>"
        if available_count == 1
        else "\n\n📊 <b>Available Lines</b>"
    ]
    for emoji, label, value in display_lines:
        spacing = "  "
        if value is not None:
            lines.append(f"\n{emoji} {label}:{spacing}<code>{value:.1f}</code>")
        elif provider_upper not in {"UNDERDOG", "PRIZEPICKS"} or label == "Player Prop":
            lines.append(
                f"\n{emoji} {label}:{spacing}<i>Unavailable for this prop</i>"
            )
    return "".join(lines)


def _evidence_display(
    validation: Optional[object],
    decision: Optional[object],
) -> tuple[str, int]:
    """Return a user-safe evidence label and completed-result count."""
    source = str(
        getattr(decision, "evidence_source", "") or ""
    ).strip().lower()
    if isinstance(decision, dict):
        source = str(
            decision.get("evidence_source")
            or decision.get("evidence_source_key")
            or ""
        ).strip().lower()
    if source in {"settled_results", "historical_average"}:
        count = (
            int(getattr(validation, "n_real_games", 0) or 0)
            + int(getattr(validation, "n_supplemental_games", 0) or 0)
        )
        if not count and source == "settled_results":
            for prefix in ("l5", "l10", "l20", "l30", "season"):
                value = getattr(decision, f"{prefix}_games", None)
                if isinstance(value, int):
                    count = value
                    break
        return evidence_source_label(source), count

    source = str(getattr(validation, "data_source", "") or "").strip().lower()
    real = int(getattr(validation, "n_real_games", 0) or 0)
    supplemental = int(getattr(validation, "n_supplemental_games", 0) or 0)
    total = real + supplemental
    if source in {"real", "primary"} and real:
        return "completed game results", real
    if source == "propline" and supplemental:
        return "PropLine results", supplemental
    if source in {"real+propline", "primary+propline"} and total:
        return "game results + PropLine results", total

    # Serialized decision evidence is accepted by the delivery gate for
    # compatibility.  Do not invent a source label when the formatter cannot
    # identify one explicitly.
    if isinstance(decision, dict):
        nested = decision.get("evidence") or decision.get("bet_evidence")
        if isinstance(nested, dict):
            nested_source = str(
                nested.get("data_source") or nested.get("source") or ""
            ).strip().lower()
            nested_count = int(
                nested.get("n_real_games")
                or nested.get("n_supplemental_games")
                or nested.get("n")
                or 0
            )
            if nested_source and nested_count:
                return nested_source.replace("_", " "), nested_count
    return "unavailable", 0


def _has_strong_directional_history(
    validation: Optional[object],
    decision: Optional[object],
) -> bool:
    """Only label a pick strong when history supports its actual direction."""
    recommendation = str(
        getattr(decision, "recommendation", "") or ""
    ).upper()
    if recommendation not in {"OVER", "UNDER"}:
        return False
    source = str(getattr(validation, "data_source", "") or "").strip().lower()
    if source not in {
        "real", "primary", "propline", "real+propline", "primary+propline",
    }:
        return False
    sample_count = (
        int(getattr(validation, "n_real_games", 0) or 0)
        + int(getattr(validation, "n_supplemental_games", 0) or 0)
    )
    if sample_count < 5:
        return False

    # Every displayed primary window must agree.  Do not let one favorable
    # window produce a STRONG BET label while L15/L20 or Season contradicts
    # the direction.
    from engine.delivery_evidence import has_strict_directional_history

    return has_strict_directional_history(decision)


def _display_label(value: object) -> str:
    """Return a human-readable enum/string label without its Python type name."""
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text.rsplit(".", 1)[-1]


def _numeric_attr(obj: Optional[object], name: str) -> Optional[float]:
    """Read a numeric display value without rendering mock/sentinel objects."""
    if isinstance(obj, dict):
        value = obj.get(name)
    else:
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


def _format_history_block(
    decision: Optional[object],
    validation: Optional[object] = None,
) -> str:
    """Render one compact hit-history block, including available H2H data."""
    if decision is None and validation is None:
        return ""

    l30_games = _numeric_attr(decision, "l30_games")
    windows = (
        ("L5", "l5", "l5_hit_rate", "l5_games", "l5_over"),
        ("L10", "l10", "l10_hit_rate", "l10_games", "l10_over"),
        ("L20", "l20", "l20_hit_rate", "l20_games", "l20_over"),
        ("L30", "l30", "l30_hit_rate", "l30_games", "l30_over"),
        ("Season", "season", "season_hit_rate", "season_games", "season_over"),
        ("H2H L5", "h2h_l5", "h2h_l5_hit_rate", "h2h_l5_games", "h2h_l5_over"),
        ("H2H L10", "h2h_l10", "h2h_l10_hit_rate", "h2h_l10_games", "h2h_l10_over"),
        ("H2H L20", "h2h_l20", "h2h_l20_hit_rate", "h2h_l20_games", "h2h_l20_over"),
        ("H2H", "h2h", "h2h_hit_rate", "h2h_games", "h2h_over"),
    )
    rows: list[str] = []
    h2h_rendered = False
    actuals_source = decision if decision is not None else validation
    if isinstance(actuals_source, dict):
        raw_recent_values = actuals_source.get("recent_values")
        raw_sample_values = actuals_source.get("sample_values")
    else:
        raw_recent_values = getattr(actuals_source, "recent_values", None)
        raw_sample_values = getattr(actuals_source, "sample_values", None)

    def _format_actuals(raw_values: object) -> list[str]:
        rendered: list[str] = []
        if isinstance(raw_values, (list, tuple)):
            for raw_value in raw_values:
                if isinstance(raw_value, bool):
                    continue
                try:
                    numeric_value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(numeric_value):
                    continue
                rendered.append(
                    f"{numeric_value:.2f}".rstrip("0").rstrip(".")
                )
        return rendered

    recent_actuals = _format_actuals(raw_recent_values[:5] if isinstance(
        raw_recent_values, (list, tuple)
    ) else ())
    if isinstance(raw_sample_values, dict):
        sample_values = raw_sample_values
    elif isinstance(raw_sample_values, (list, tuple)):
        sample_values = {
            str(item[0]): item[1]
            for item in raw_sample_values
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
            )
        }
    else:
        sample_values = {}

    sample_values_rendered = False
    direction = _decision_recommendation(decision)
    for label, values_key, rate_name, games_name, over_name in windows:
        rate = _numeric_attr(decision, rate_name)
        games = _numeric_attr(decision, games_name)
        over = _numeric_attr(decision, over_name)
        if direction == "UNDER":
            under = _numeric_attr(decision, over_name.replace("_over", "_under"))
            if under is not None and games:
                over = under
                rate = under / games
            else:
                # Without strict UNDER counts, pushes cannot be distinguished
                # from losses. Never report 1 - OVER as verified UNDER history.
                over = None
                rate = None
        shown_label = _history_window_label(label, games, l30_games)
        if rate is None or games is None or games <= 0:
            if label in {"L5", "L10", "L20", "L30"}:
                sample_size = _numeric_attr(decision, "season_games")
                detail = (
                    f" — {int(sample_size)} valid games available"
                    if sample_size is not None and 0 < sample_size < int(label[1:])
                    else ""
                )
                rows.append(f"   <b>{shown_label}:</b> N/A{detail}")
            continue
        if over is not None:
            value = f"{rate:.0%} ({int(over)}/{int(games)})"
        else:
            value = f"{rate:.0%} ({int(games)} games)"
        actuals = _format_actuals(sample_values.get(values_key))
        if values_key == "l5" and not actuals:
            actuals = recent_actuals
        if actuals:
            value = f"{', '.join(actuals)} · {value}"
            sample_values_rendered = True
        rows.append(f"   <b>{shown_label}:</b> {value} ({direction})" if direction in {"OVER", "UNDER"} else f"   <b>{shown_label}:</b> {value}")
        if label == "H2H":
            h2h_rendered = True

    if recent_actuals and not sample_values_rendered:
        rows.append(
            f"   <b>Recent actuals:</b> {', '.join(recent_actuals)}"
        )

    # Compatibility delivery callers may provide a validated history snapshot
    # without a fully materialized UDBetDecision.  Keep the alert historical
    # rather than falling back to an uninformative N/A block.
    if not rows and validation is not None:
        for label, rate_name, games_name in (
            ("L5", "l5_rate", "l5_games"),
            ("L10", "l10_rate", "l10_games"),
            ("L20", "l20_rate", "l20_games"),
            ("L30", "l30_rate", "l30_games"),
            ("Season", "season_hit_rate", "season_games"),
        ):
            rate = _numeric_attr(validation, rate_name)
            games = _numeric_attr(validation, games_name)
            if rate is None or games is None or games <= 0:
                continue
            rows.append(f"   <b>{label}:</b> {rate:.0%} ({int(games)} games)")

        if not rows:
            source, count = _evidence_display(validation, decision)
            if count > 0 and source != "unavailable":
                rows.append(
                    f"   <b>Results:</b> {escape(source)} ({count} games)"
                )

    # Some delivery paths have H2H on PlayerPropValidation before a complete
    # UDBetDecision is available. Preserve that signal rather than silently
    # dropping it from the Telegram alert.
    validation_h2h = _numeric_attr(validation, "h2h_hit_rate")
    if not h2h_rendered and validation_h2h is not None:
        rows.append(f"   <b>H2H:</b> {validation_h2h:.0%}")
    elif not h2h_rendered:
        rows.append("   <b>H2H:</b> N/A")

    if not rows:
        return "\n\n📈 <b>HISTORY</b>\n   <b>L5:</b> N/A — no history available"
    return "\n\n📈 <b>HISTORY</b>\n" + "\n".join(rows)


def _format_value_analysis_block(
    *,
    player_name: str,
    sport: str,
    stat_type: str,
    line: float,
    source: str,
    decision: Optional[object],
    validation: Optional[object] = None,
    comparison_line: Optional[float] = None,
    comparison_source: Optional[str] = None,
    comparison_odds: Optional[int] = None,
    game_time: Optional[datetime] = None,
    team: Optional[str] = None,
    opponent: Optional[str] = None,
    market_confirmation: Optional[dict] = None,
    intelligence_trace: Optional[dict] = None,
) -> str:
    """Render an evidence-bound value explanation in the user's preferred shape.

    This is deliberately a formatter, not a second scoring engine.  It compares
    only explicitly supplied lines/odds and uses the stored hit-rate windows.
    Missing comparison data stays unavailable instead of becoming a fabricated
    value claim.
    """
    direction = str(getattr(decision, "recommendation", "") or "").upper()
    if direction not in {"OVER", "UNDER"}:
        return ""

    primary_source = str(source or "Player Props").strip() or "Player Props"
    line_text = f"{float(line):g}"
    title = (
        f"[{escape(str(sport or 'UNKNOWN'))}] "
        f"{escape(str(player_name))} {direction} {line_text} "
        f"{escape(str(stat_type))}"
    )
    lines = ["\n🤖 <b>AI ANALYSIS</b>", title]

    compare = comparison_line
    compare_label = str(comparison_source or "").strip()
    compare_odds = comparison_odds
    if compare_odds is None and isinstance(market_confirmation, dict):
        raw_odds = market_confirmation.get("avg_odds")
        try:
            compare_odds = int(raw_odds) if raw_odds is not None else None
        except (TypeError, ValueError):
            compare_odds = None
    if compare is None and isinstance(market_confirmation, dict):
        raw_compare = market_confirmation.get("avg_line")
        try:
            if raw_compare is not None:
                compare = float(raw_compare)
        except (TypeError, ValueError):
            compare = None
        compare_label = compare_label or str(
            market_confirmation.get("source")
            or market_confirmation.get("provider")
            or "market confirmation"
        ).strip()

    if compare is not None and compare_label:
        try:
            diff = abs(float(compare) - float(line))
        except (TypeError, ValueError):
            diff = None
        odds_text = (
            f" with odds of {format_odds(compare_odds)}"
            if compare_odds is not None
            else ""
        )
        lines.append(
            f"This line is set at <b>{line_text}</b> on "
            f"{escape(primary_source)}"
            f"{' for a fixed payout' if primary_source.upper() == 'PRIZEPICKS' else ''}, but "
            f"{escape(compare_label)} has it at <b>{float(compare):g}</b>"
            f"{odds_text}."
        )
        favorable_gap = (
            (direction == "OVER" and float(line) < float(compare))
            or (direction == "UNDER" and float(line) > float(compare))
        )
        if favorable_gap:
            lines.append(
                f"Taking the {direction.lower()} on {escape(primary_source)} "
                f"at {line_text} may be good value here, but double check "
                "alternate lines and current stats before placing it."
            )
        else:
            lines.append(
                "The available comparison does not show a clear line-value "
                "advantage, so verify alternate lines and current stats first."
            )
        if diff is not None:
            lines.append(f"<b>Diff:</b> {diff:g}")
    else:
        lines.append(
            f"This line is set at <b>{line_text}</b> on "
            f"{escape(primary_source)}, but no independent comparison line "
            "was available. No odds-backed value claim is being made."
        )

    if game_time is not None:
        lines.append(
            f"<b>Time:</b> {_format_game_time_et(game_time)}"
        )
    game_parts = [
        str(value).strip()
        for value in (team, opponent)
        if value is not None and str(value).strip()
    ]
    if game_parts:
        lines.append(f"<b>Game:</b> {escape(' vs '.join(game_parts))}")

    def _direction_rate(
        rate_name: str,
        games_name: str,
        over_name: str,
    ) -> Optional[float]:
        rate = _numeric_attr(decision, rate_name)
        games = _numeric_attr(decision, games_name)
        over = _numeric_attr(decision, over_name)
        if rate is None and games and over is not None:
            rate = over / games
        if rate is None:
            return None
        # Stored rates are OVER-perspective rates.  Show the rate for the
        # actual recommendation so UNDER is not mislabeled as support.
        return 1.0 - rate if direction == "UNDER" else rate

    history_rows = (
        ("L10", "l10_hit_rate", "l10_games", "l10_over"),
        ("H2H", "h2h_hit_rate", "h2h_games", "h2h_over"),
        ("SZN", "season_hit_rate", "season_games", "season_over"),
    )
    for label, rate_name, games_name, over_name in history_rows:
        rate = _direction_rate(rate_name, games_name, over_name)
        if rate is None:
            lines.append(f"<b>{label} ({direction.title()}):</b> N/A")
        else:
            lines.append(f"<b>{label} ({direction.title()}):</b> {rate:.0%}")

    team_ml = None
    for payload in (market_confirmation, intelligence_trace):
        if isinstance(payload, dict):
            team_ml = payload.get("team_ml") or payload.get("team_moneyline")
            if team_ml:
                break
    if isinstance(team_ml, dict):
        ml_odds = team_ml.get("odds")
        ml_probability = team_ml.get("implied_probability")
        if ml_odds is not None or ml_probability is not None:
            ml_parts = []
            if ml_odds is not None:
                ml_parts.append(format_odds(int(ml_odds)))
            if ml_probability is not None:
                try:
                    ml_parts.append(f"{float(ml_probability):.0%}")
                except (TypeError, ValueError):
                    pass
            if ml_parts:
                lines.append(f"<b>Team ML (IP):</b> {' '.join(ml_parts)}")

    return "\n".join(lines)


def _format_quality_line(
    decision: Optional[object],
    score: Optional[object],
) -> str:
    """Compatibility stub; confidence/quality is never rendered in Telegram."""
    return ""


def _format_market_quality_line(market_quality: Optional[object]) -> str:
    """Compatibility stub; market quality is never rendered in Telegram."""
    return ""


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

    Legacy tier/quality metadata is not part of the analyst input. The
    narrative remains advisory and cannot affect delivery.
    """
    if decision is None:
        return ""
    rec       = getattr(decision, "recommendation", None)
    raw_tier  = ""
    conf      = 0
    rlvl      = "MEDIUM"

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
    gt = (
        game_time.astimezone(timezone.utc).replace(tzinfo=None)
        if game_time.tzinfo is not None and game_time.utcoffset() is not None
        else game_time.replace(tzinfo=None)
    )
    mins_until = (gt - datetime.utcnow()).total_seconds() / 60
    if mins_until <= 0:
        return "🔴 GAME LIVE / CLOSING"
    elif mins_until <= 30:
        return "🟡 CLOSING SOON"
    else:
        return "🟢 AVAILABLE NOW"


def _decision_recommendation(decision: Optional[object]) -> str:
    """Return the directional recommendation without exposing model metadata."""
    if isinstance(decision, dict):
        value = decision.get("recommendation")
    else:
        value = getattr(decision, "recommendation", None) if decision else None
    return str(value or "").strip().upper()


def _format_simple_prop_alert(
    *,
    player_name: str,
    sport: str,
    stat_type: str,
    line: float,
    game_time: Optional[datetime],
    decision: Optional[object],
    validation: Optional[object],
    source: str = "Underdog",
    opponent: Optional[str] = None,
    offering_context: Optional[object] = None,
) -> str:
    """Render the minimal user-facing player-prop alert."""
    sport_icon = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾",
        "NHL": "🏒", "UFC": "🥊", "WNBA": "🏀",
    }.get(str(sport).upper(), "🎯")
    recommendation = _decision_recommendation(decision)
    line_text = (
        f"{recommendation} {line:.1f}"
        if recommendation in {"OVER", "UNDER"}
        else f"{line:.1f}"
    )
    history = _format_history_block(decision, validation)
    if history:
        history = history.lstrip("\n")
    else:
        history = "📈 <b>History:</b>\n" + "\n".join(
            f"   <b>{window}:</b> N/A"
            for window in ("L5", "L10", "L20", "L30", "H2H")
        )
    from engine.offering_coverage import coverage
    offering = offering_context or coverage.match(
        provider=source, player=player_name, sport=sport, stat=stat_type,
        line=line, game_time=game_time,
    )
    variant = str(getattr(offering, "line_variant", "") or "classification unavailable")
    comparison = []
    if offering is not None:
        counterparts = coverage.comparisons(offering)
        # Display one verified reference to keep Telegram messages bounded;
        # every alternate offering remains in discovery/evaluation/inventory.
        counterparts.sort(key=lambda row: (
            row.line_variant != offering.line_variant,
            -row.line_value if recommendation == "OVER" else row.line_value,
        ))
        for other in counterparts[:1]:
            other_variant = str(other.line_variant or "classification unavailable")
            difference = abs(line - other.line_value)
            favorable = (
                recommendation == "OVER" and line < other.line_value
                or recommendation == "UNDER" and line > other.line_value
            )
            comparison.append(
                f"This line is set at {line:g} on {escape(source)} ({escape(variant)}) "
                f"for a fixed payout, but {escape(other.provider)} has "
                f"{escape(stat_type)} at {other.line_value:g} ({escape(other_variant)}). "
                f"Taking the {recommendation} on {escape(source)} at {line:g} "
                + ("may offer a line advantage" if favorable else "does not show a directional line advantage")
                + "; a line difference alone does not establish profitable value. "
                "Verify available alt lines and player statistics."
                + (
                    " Different line classifications may have different payout or settlement terms."
                    if variant != other_variant else ""
                )
                + f"\n<b>Diff:</b> {difference:g}"
                f"\n<b>Other directions:</b> {', '.join(other.available_directions)}"
            )
    game = str(getattr(offering, "event", "") or opponent or "N/A — matchup unavailable")

    return "\n".join(
        [
            "📊 <b>PLAYER PROP</b>",
            "",
            f"{sport_icon} <b>{escape(str(sport))}</b>",
            f"👤 <b>Player:</b> {escape(str(player_name))}",
            f"📊 <b>Stat:</b> {escape(str(stat_type))}",
            f"🎯 <b>Line:</b> <code>{line_text}</code>",
            f"<b>Platform:</b> {escape(source)} · fixed payout · {escape(variant)}",
            f"<b>Game:</b> {escape(game)}",
            (
                f"🕐 <b>Game starts:</b> {_format_game_time_et(game_time)}"
                if game_time is not None
                else "🕐 <b>Game starts:</b> N/A"
            ),
            "",
            "\n\n".join(comparison) if comparison else "Cross-platform comparison: unavailable or not verified.",
            history,
            "",
            "Monitoring player props across multiple sports. Not betting advice.",
        ]
    )


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
    market_confirmation: Optional[dict] = None,  # optional market confirmation data
    source: str = "Underdog",
    offering_context: Optional[object] = None,
) -> str:
    """Format a compact player-prop alert with only the actionable details."""
    if removed:
        return "\n".join(
            [
                "🚫 <b>PLAYER PROP REMOVED</b>",
                "",
                f"👤 <b>Player:</b> {escape(str(player_name))}",
                f"📊 <b>Stat:</b> {escape(str(stat_type))}",
                f"🎯 <b>Last line:</b> <code>{old_line:.1f}</code>",
                f"Reason: {escape(removal_reason or 'Market no longer available')}",
            ]
        )

    return _format_simple_prop_alert(
        player_name=player_name,
        sport=sport,
        stat_type=stat_type,
        line=new_line,
        game_time=game_time,
        decision=decision,
        validation=validation,
        source=source,
        opponent=opponent,
        offering_context=offering_context,
    )


def format_market_move_detected(
    player_name: str,
    team: str,
    sport: str,
    stat_type: str,
    old_line: float,
    new_line: float,
    game_time: Optional[datetime] = None,
) -> str:
    """Lightweight player-prop alert for a significant line move.

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
        f"🎯 <b>LINE:</b>  <code>{old_line:.1f}</code>  →  <code>{new_line:.1f}</code>",
        f"📉 <b>Movement:</b>  <code>{change_sign}{change:.1f}</code>",
        "",
        "📊 <b>Status:</b>  Tracking — evaluating for a directional pick",
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
    market_confirmation: Optional[dict] = None,  # optional market confirmation data
    source: str = "Underdog",
    offering_context: Optional[object] = None,
) -> str:
    """Format a compact first-appearance player-prop alert."""
    return _format_simple_prop_alert(
        player_name=player_name,
        sport=sport,
        stat_type=stat_type,
        line=line_value,
        game_time=game_time,
        decision=decision,
        validation=validation,
        source=source,
        opponent=opponent,
        offering_context=offering_context,
    )


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

    Sorted display order: 0.5 lines first → line ascending.
       Immediately-alerted props are marked with ⚡; internal quality metadata
       is used only for ordering and is not shown.
    """
    if not new_props:
        return ""

    total       = len(new_props)
    n_immediate = sum(1 for p in new_props if p.get("immediate"))
    n_half_line = sum(1 for p in new_props if (p.get("line") or 99.0) <= 0.5)

    def _sort_key(p: dict) -> tuple:
        line  = p.get("line") or 99.0
        return (0 if line <= 0.5 else 1, line)

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
        sport  = p.get("sport", "")
        icon   = sport_icon.get(sport, "🎯")
        line   = p.get("line") or 0.0
        marker = "⚡" if p.get("immediate") else "•"
        parts.append(
            f"  {marker}  {icon} {p['player']} — {p['stat_type']}"
            f"  @{line}"
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
