"""
prizepicks.py — PrizePicks player prop monitoring and sportsbook comparison.

Fetches PrizePicks projections via the public API, stores line history, and
compares them against sportsbook fair probabilities to detect +EV edges.

PrizePicks pays even-money per side (50% implied per leg in isolation).
Edge formula:
    edge_over  = (adjusted_fair_prob_over  − 0.5) × 100  [positive → bet OVER]
    edge_under = (adjusted_fair_prob_under − 0.5) × 100  [positive → bet UNDER]

When PP line ≠ sportsbook line the fair probability is adjusted using a
per-stat linear model (``_PROB_PER_UNIT``).  This is a first-order approximation;
production models should use per-player historical distributions.

Does NOT touch: EV engine, Steam engine, Confidence engine, AlertDelivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

import aiohttp

logger = logging.getLogger(__name__)


# ── League / sport mappings ────────────────────────────────────────────────────

# PrizePicks internal league IDs
PP_LEAGUE_IDS: dict[str, int] = {
    "NBA":   2,
    "NFL":   9,
    "MLB":   3,
    "NHL":   8,
    "NCAAF": 5,
    "NCAAB": 10,
    "MLS":   7,
}

# PrizePicks stat name → The Odds API player-prop market name
PP_STAT_TO_ODDS_API: dict[str, str] = {
    "Points":           "player_points",
    "Rebounds":         "player_rebounds",
    "Assists":          "player_assists",
    "Steals":           "player_steals",
    "Blocks":           "player_blocks",
    "Threes Made":      "player_threes",
    "Passing Yards":    "player_pass_yds",
    "Rushing Yards":    "player_rush_yds",
    "Receiving Yards":  "player_reception_yds",
    "Receptions":       "player_receptions",
    "Touchdowns":       "player_pass_tds",
    "Shots on Goal":    "player_shots_on_target",
    "Goals":            "player_goal_scorer_anytime",
    "Hits":             "player_hits",
    "Strikeouts":       "player_pitcher_strikeouts",
    "Bases":            "player_total_bases",
}

# Approximate probability change per 1 stat unit near the median.
# Used to adjust fair probability from sportsbook line to PP line.
# Derived from typical per-sport stat standard deviations.
_PROB_PER_UNIT: dict[str, float] = {
    "Points":         2.5,   # NBA σ ≈ 4 pts near median
    "Rebounds":       5.0,
    "Assists":        5.0,
    "Steals":        10.0,
    "Blocks":        10.0,
    "Threes Made":    8.0,
    "Passing Yards":  0.4,   # NFL σ ≈ 60 yds
    "Rushing Yards":  1.5,
    "Receiving Yards":1.5,
    "Receptions":     6.0,
    "Touchdowns":    15.0,
    "Shots on Goal":  7.0,
    "Goals":         12.0,
    "Hits":           8.0,
    "Strikeouts":     7.0,
    "Bases":          5.0,
}
_DEFAULT_PROB_PER_UNIT = 3.0


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class PrizePicksLine:
    """A single PrizePicks player prop projection."""
    external_id: str
    player_name: str
    team: str
    sport: str                     # "NBA", "NFL", …
    league: str
    stat_type: str                 # "Points", "Rebounds", …
    line_value: float              # the over/under value
    start_time: Optional[datetime] = None
    game_description: str = ""
    fetched_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PPEdgeOpportunity:
    """
    A detected edge between a PrizePicks prop line and sportsbook odds.

    edge_over / edge_under are in percentage points.
    A positive value means PP offers positive EV for that side.
    """
    pp_line: PrizePicksLine
    sportsbook: str
    sportsbook_line: float          # reference sportsbook line value
    sportsbook_over_odds: int       # American odds for OVER at sportsbook_line
    sportsbook_under_odds: int      # American odds for UNDER at sportsbook_line
    fair_prob_over_at_sb_line: float    # vig-removed at the sportsbook line
    fair_prob_under_at_sb_line: float
    line_diff: float                # sportsbook_line − pp_line.line_value
    adjusted_fair_prob_over: float  # adjusted to the PP line value
    adjusted_fair_prob_under: float
    edge_over: float                # % edge for OVER side (positive = bet OVER)
    edge_under: float               # % edge for UNDER side
    best_side: Literal["OVER", "UNDER"]
    best_edge: float                # max(edge_over, edge_under)
    prob_per_unit: float            # adjustment factor used


# ── Math helpers (self-contained — no engine import required) ─────────────────

def _american_to_implied(odds: int) -> float:
    """Convert American odds to implied probability (with vig)."""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _fair_prob_multiplicative(
    over_odds: int, under_odds: int
) -> tuple[float, float]:
    """
    Remove vig from a two-way market using the multiplicative method.
    Returns (fair_prob_over, fair_prob_under) that sum to 1.0.
    """
    p_over  = _american_to_implied(over_odds)
    p_under = _american_to_implied(under_odds)
    total   = p_over + p_under
    if total <= 0:
        return 0.5, 0.5
    return p_over / total, p_under / total


def _prob_per_unit_for(stat_type: str) -> float:
    return _PROB_PER_UNIT.get(stat_type, _DEFAULT_PROB_PER_UNIT)


# ── Core comparison logic ─────────────────────────────────────────────────────

def compare_pp_to_sportsbook(
    pp_line: PrizePicksLine,
    *,
    sportsbook: str,
    sb_line: float,
    sb_over_odds: int,
    sb_under_odds: int,
) -> PPEdgeOpportunity:
    """
    Compare a PrizePicks prop line against sportsbook over/under odds.

    Steps:
      1. Remove vig from sportsbook odds → fair probabilities at ``sb_line``.
      2. If lines differ, linearly adjust the fair probability to ``pp_line.line_value``
         using the per-stat probability-per-unit model.
      3. Compute edge as adjusted_fair_prob − 0.5 (PP implied) for each side.

    Args:
        pp_line:       PrizePicks projection to evaluate.
        sportsbook:    Reference sportsbook name.
        sb_line:       Sportsbook's over/under number (may differ from PP line).
        sb_over_odds:  American odds for OVER at ``sb_line``.
        sb_under_odds: American odds for UNDER at ``sb_line``.

    Returns:
        PPEdgeOpportunity with edge in percentage points.
        Positive edge_over  → PP OVER is +EV.
        Positive edge_under → PP UNDER is +EV.
    """
    fair_over, fair_under = _fair_prob_multiplicative(sb_over_odds, sb_under_odds)

    # Line adjustment
    line_diff  = sb_line - pp_line.line_value   # + → PP bar is lower → OVER easier
    ppu        = _prob_per_unit_for(pp_line.stat_type)
    adjustment = line_diff * (ppu / 100.0)      # fraction of probability

    adj_over  = min(max(fair_over  + adjustment, 0.01), 0.99)
    adj_under = min(max(fair_under - adjustment, 0.01), 0.99)

    edge_over  = round((adj_over  - 0.5) * 100, 2)
    edge_under = round((adj_under - 0.5) * 100, 2)
    best_side  = "OVER" if edge_over >= edge_under else "UNDER"
    best_edge  = round(max(edge_over, edge_under), 2)

    return PPEdgeOpportunity(
        pp_line=pp_line,
        sportsbook=sportsbook,
        sportsbook_line=sb_line,
        sportsbook_over_odds=sb_over_odds,
        sportsbook_under_odds=sb_under_odds,
        fair_prob_over_at_sb_line=round(fair_over,  4),
        fair_prob_under_at_sb_line=round(fair_under, 4),
        line_diff=round(line_diff, 2),
        adjusted_fair_prob_over=round(adj_over,  4),
        adjusted_fair_prob_under=round(adj_under, 4),
        edge_over=edge_over,
        edge_under=edge_under,
        best_side=best_side,
        best_edge=best_edge,
        prob_per_unit=ppu,
    )


# ── PrizePicks API client ─────────────────────────────────────────────────────

class PrizePicksClient:
    """
    Async client for the PrizePicks public projections API.

    No authentication required; uses the public JSON:API endpoint.

    Usage::

        async with PrizePicksClient() as client:
            lines = await client.fetch_projections(PP_LEAGUE_IDS["NBA"])
    """

    BASE_URL = "https://api.prizepicks.com/projections"
    HEADERS  = {
        "User-Agent":  "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)",
        "Accept":      "application/json",
        "Referer":     "https://app.prizepicks.com/",
    }
    _TIMEOUT = aiohttp.ClientTimeout(total=15)

    def __init__(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._session     = session
        self._owns_session = session is None

    async def __aenter__(self) -> "PrizePicksClient":
        if self._owns_session:
            self._session = aiohttp.ClientSession(headers=self.HEADERS)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_projections(
        self,
        league_id: int,
        *,
        per_page: int = 250,
        single_stat: bool = True,
    ) -> list[PrizePicksLine]:
        """
        Fetch current projections for a league from the PrizePicks API.

        Returns an empty list (and logs a warning) on any error so background
        jobs can continue past transient failures.
        """
        if self._session is None:
            raise RuntimeError(
                "PrizePicksClient must be used as an async context manager."
            )

        params = {
            "league_id":    league_id,
            "per_page":     per_page,
            "single_stat":  "true" if single_stat else "false",
            "in_the_game":  "false",
        }

        try:
            async with self._session.get(
                self.BASE_URL, params=params, timeout=self._TIMEOUT
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "PrizePicks API HTTP %d for league_id=%d",
                        resp.status, league_id,
                    )
                    return []
                raw = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            logger.warning("PrizePicks request error (league_id=%d): %s", league_id, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("PrizePicks unexpected error (league_id=%d): %s", league_id, exc)
            return []

        return self._parse(raw, league_id)

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse(self, data: dict, league_id: int) -> list[PrizePicksLine]:
        """Parse a JSON:API response from PrizePicks into PrizePicksLine objects."""
        included     = data.get("included", [])
        players: dict[str, dict]  = {}
        leagues: dict[str, dict]  = {}

        for item in included:
            t = item.get("type")
            if t == "new_player":
                players[item["id"]] = item.get("attributes", {})
            elif t == "league":
                leagues[item["id"]] = item.get("attributes", {})

        lines: list[PrizePicksLine] = []
        for proj in data.get("data", []):
            if proj.get("type") != "projection":
                continue

            attrs = proj.get("attributes", {})
            status = attrs.get("status", "")
            if status and status not in ("pre_game", "in_progress"):
                continue

            line_score = attrs.get("line_score")
            if line_score is None:
                continue

            rels      = proj.get("relationships", {})
            player_id = rels.get("new_player", {}).get("data", {}).get("id", "")
            p_attrs   = players.get(player_id, {})

            league_id_s = rels.get("league", {}).get("data", {}).get("id", str(league_id))
            l_attrs     = leagues.get(league_id_s, {})
            league_name = l_attrs.get("name", str(league_id))

            start_time: Optional[datetime] = None
            raw_t = attrs.get("start_time")
            if raw_t:
                try:
                    from dateutil.parser import parse as _dp
                    start_time = _dp(raw_t).replace(tzinfo=None)
                except Exception:
                    pass

            lines.append(PrizePicksLine(
                external_id=proj.get("id", ""),
                player_name=p_attrs.get("name", "Unknown Player"),
                team=p_attrs.get("team_name", p_attrs.get("team", "")),
                sport=league_name,
                league=league_name,
                stat_type=attrs.get("stat_type", ""),
                line_value=float(line_score),
                start_time=start_time,
                game_description=attrs.get("description", ""),
                fetched_at=datetime.utcnow(),
            ))

        logger.debug(
            "PrizePicks: parsed %d projections for league_id=%d", len(lines), league_id
        )
        return lines
