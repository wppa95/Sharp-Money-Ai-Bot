"""
providers/tennis_stats.py — Player stat history for Tennis.

Data source: Jeff Sackmann's public tennis match datasets on GitHub.
  ATP: https://github.com/JeffSackmann/tennis_atp  (atp_matches_{year}.csv)
  WTA: https://github.com/JeffSackmann/tennis_wta  (wta_matches_{year}.csv)

These CSVs are updated 1–3 days after each tournament match completes.
No API key required.

Supported Underdog stat types
──────────────────────────────
  aces              → w_ace / l_ace
  double faults     → w_df / l_df
  first serves in   → w_1stIn / l_1stIn
  service points    → w_svpt / l_svpt
  total games       → computed from match score string
  games won         → computed from match score string
  sets won / sets   → computed from match score string

Architecture mirrors PlayerStatsProvider:
  TennisStatsProvider.fetch_results(player_name, sport, stat_type)
      → list[RawGameResult]
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime
from typing import Optional

import aiohttp

from providers.player_stats import RawGameResult, _names_match

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)

_ATP_URL = (
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp"
    "/master/atp_matches_{year}.csv"
)
_WTA_URL = (
    "https://raw.githubusercontent.com/JeffSackmann/tennis_wta"
    "/master/wta_matches_{year}.csv"
)

# Underdog stat-type (lower) → (winner_col, loser_col)
# Columns prefixed with "_" are computed internally from the score string.
_TENNIS_STAT_MAP: dict[str, tuple[str, str]] = {
    "aces":             ("w_ace",    "l_ace"),
    "ace":              ("w_ace",    "l_ace"),
    "double faults":    ("w_df",     "l_df"),
    "double fault":     ("w_df",     "l_df"),
    "first serves in":  ("w_1stIn",  "l_1stIn"),
    "first serve in":   ("w_1stIn",  "l_1stIn"),
    "1st serves in":    ("w_1stIn",  "l_1stIn"),
    "service points":   ("w_svpt",   "l_svpt"),
    "games won":        ("_games_w", "_games_l"),
    "total games":      ("_games_w", "_games_l"),
    "sets won":         ("_sets_w",  "_sets_l"),
    "sets":             ("_sets_w",  "_sets_l"),
}


# ── Score parsing helpers ─────────────────────────────────────────────────────

def _parse_score(score: str) -> Optional[tuple[int, int]]:
    """
    Parse a tennis match score (e.g. "6-3 7-5" or "6-3 4-6 7-5") into
    (winner_games, loser_games).  Returns None for retirements / walkovers.
    """
    if not score:
        return None
    low = score.lower()
    if any(x in low for x in ("ret", "w/o", "walkover", "def", "bye", "unfinished")):
        return None

    w_games = l_games = 0
    for set_score in score.split():
        # Strip tiebreak suffix like "(7)"
        set_score = set_score.split("(")[0]
        parts = set_score.split("-")
        if len(parts) != 2:
            return None
        try:
            wg, lg = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        w_games += wg
        l_games += lg

    return (w_games, l_games)


def _parse_sets(score: str) -> Optional[tuple[int, int]]:
    """
    Parse (winner_sets, loser_sets) from a match score string.
    Returns None for retirements / walkovers.
    """
    if not score:
        return None
    low = score.lower()
    if any(x in low for x in ("ret", "w/o", "walkover", "def", "bye", "unfinished")):
        return None

    w_sets = l_sets = 0
    for set_score in score.split():
        set_score = set_score.split("(")[0]
        parts = set_score.split("-")
        if len(parts) != 2:
            return None
        try:
            wg, lg = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if wg > lg:
            w_sets += 1
        elif lg > wg:
            l_sets += 1
        # draws don't happen in tennis sets
    return (w_sets, l_sets)


def _tourney_date_to_iso(date_str: str) -> Optional[str]:
    """Convert YYYYMMDD → YYYY-MM-DD; return None on failure."""
    try:
        dt = datetime.strptime(date_str.strip(), "%Y%m%d")
        return dt.date().isoformat()
    except (ValueError, AttributeError):
        return None


# ── Provider ──────────────────────────────────────────────────────────────────

class TennisStatsProvider:
    """
    Fetches per-match tennis stats from Jeff Sackmann's CSV datasets.
    Returns [] on any error — never raises.
    """

    def __init__(self) -> None:
        # In-memory CSV cache: url → raw text (avoids re-fetching within session)
        self._csv_cache: dict[str, str] = {}

    # ── Public entry point ────────────────────────────────────────────────────

    async def fetch_results(
        self,
        player_name: str,
        sport: str,        # "TENNIS"
        stat_type: str,
    ) -> list[RawGameResult]:
        """
        Fetch recent match results for *player_name* × *stat_type*.
        Returns [] on any error — never raises.
        """
        stat_lower = stat_type.lower().strip()

        try:
            stat_spec = _TENNIS_STAT_MAP.get(stat_lower)
            if stat_spec is None:
                logger.debug("Tennis: no mapping for stat %r", stat_lower)
                return []

            year = datetime.utcnow().year
            rows = await self._load_player_rows(player_name, year)
            if not rows:
                return []

            results: list[RawGameResult] = []
            for row in rows:
                result = self._extract_result(row, player_name, stat_lower, stat_spec)
                if result is not None:
                    results.append(result)

            # Sort newest-first (consistent with other providers)
            results.sort(key=lambda r: r.game_date, reverse=True)

            logger.debug(
                "Tennis: %s / %s → %d results",
                player_name, stat_lower, len(results),
            )
            return results

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TennisStatsProvider: unexpected error for %r %r: %s",
                player_name, stat_type, exc,
            )
            return []

    # ── CSV loading ───────────────────────────────────────────────────────────

    async def _load_player_rows(
        self, player_name: str, year: int
    ) -> list[dict]:
        """
        Load rows matching *player_name* from ATP then WTA datasets.
        Tries the current year first; falls back to previous year if no rows
        found (handles off-season / early-season gaps).
        """
        for circuit_url_template in [_ATP_URL, _WTA_URL]:
            for y in [year, year - 1]:
                url  = circuit_url_template.format(year=y)
                rows = await self._fetch_filtered_csv(url, player_name)
                if rows:
                    return rows
        return []

    async def _fetch_filtered_csv(
        self, url: str, player_name: str
    ) -> list[dict]:
        """Return CSV rows where *player_name* appears as winner or loser."""
        if url not in self._csv_cache:
            raw = await self._get_text(url)
            if raw is None:
                return []
            self._csv_cache[url] = raw

        text = self._csv_cache[url]
        try:
            reader = csv.DictReader(io.StringIO(text))
            rows   = []
            for row in reader:
                wn = row.get("winner_name", "")
                ln = row.get("loser_name",  "")
                if _names_match(wn, player_name) or _names_match(ln, player_name):
                    rows.append(row)
            return rows
        except Exception as exc:
            logger.debug("Tennis: CSV parse error for %s: %s", url, exc)
            return []

    # ── Row → RawGameResult ───────────────────────────────────────────────────

    @staticmethod
    def _extract_result(
        row: dict,
        player_name: str,
        stat_lower: str,
        stat_spec: tuple[str, str],
    ) -> Optional[RawGameResult]:
        winner_name = row.get("winner_name", "")
        loser_name  = row.get("loser_name",  "")
        player_won  = _names_match(winner_name, player_name)

        winner_col, loser_col = stat_spec
        col   = winner_col if player_won else loser_col
        score = row.get("score", "")

        if col in ("_games_w", "_games_l"):
            parsed = _parse_score(score)
            if parsed is None:
                return None
            val = float(parsed[0] if player_won else parsed[1])

        elif col in ("_sets_w", "_sets_l"):
            parsed = _parse_sets(score)
            if parsed is None:
                return None
            val = float(parsed[0] if player_won else parsed[1])

        else:
            raw = row.get(col, "")
            if raw in ("", None, "NA", "N/A"):
                return None
            try:
                val = float(raw)
            except (ValueError, TypeError):
                return None

        game_date = _tourney_date_to_iso(row.get("tourney_date", ""))
        if not game_date:
            return None

        opponent = (loser_name if player_won else winner_name) or None

        return RawGameResult(
            player_name  = player_name,
            sport        = "TENNIS",
            stat_type    = stat_lower,
            game_date    = game_date,
            actual_value = val,
            opponent     = opponent,
            source       = "sackmann_csv",
        )

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get_text(self, url: str) -> Optional[str]:
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)"},
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            "TennisStatsProvider: HTTP %d for %s", resp.status, url
                        )
                        return None
                    return await resp.text(encoding="utf-8", errors="replace")
        except asyncio.TimeoutError:
            logger.debug("TennisStatsProvider: timeout for %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.debug("TennisStatsProvider: client error for %s: %s", url, exc)
            return None
