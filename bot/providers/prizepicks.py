"""
PrizePicks provider — concrete PropProviderBase implementation.

Since the PrizePicks API is protected by DataDome, this provider currently
operates in "manual feed" mode only via PrizePicksManualProvider.

When DataDome is bypassed or an alternative data source becomes available:
  1.  Implement auth/proxy bypass in PrizePicksClient (bot/prizepicks.py).
  2.  Override PrizePicksProvider.is_available() to return True.
  3.  The rest of the system picks up PlayerProp objects automatically.

Data flow:
    Manual JSON feed  ──┐
    Future API feed   ──┤
    Cached feed       ──┴─► PrizePicksProvider (this file)
                                    ↓
                              PlayerProp  (normalized, provider-agnostic)
                                    ↓
                             PropLineHistory  (database)
                                    ↓
                        PropComparisonEngine  →  alerts
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from providers.prop_provider import PlayerProp, PropProviderBase

# Import the existing PrizePicks data models (bot/prizepicks.py)
try:
    from prizepicks import PrizePicksLine  # type: ignore[import]
    _PRIZEPICKS_MODULE = True
except ImportError:  # pragma: no cover
    _PRIZEPICKS_MODULE = False
    PrizePicksLine = None  # type: ignore[assignment,misc]


# ── Stat type normalization ───────────────────────────────────────────────────

_STAT_NORM: dict[str, str] = {
    # Basketball
    "pts":             "points",
    "points":          "points",
    "reb":             "rebounds",
    "rebounds":        "rebounds",
    "ast":             "assists",
    "assists":         "assists",
    "3pm":             "3-pointers made",
    "blk":             "blocks",
    "stl":             "steals",
    # Baseball
    "hits":            "hits",
    "hr":              "home runs",
    "home runs":       "home runs",
    "rbis":            "rbis",
    "strikeouts":      "strikeouts",
    "walks":           "walks",
    "total bases":     "total bases",
    # American football
    "rushing yards":   "rushing yards",
    "receiving yards": "receiving yards",
    "receptions":      "receptions",
    "passing yards":   "passing yards",
    "passing tds":     "passing tds",
    "rush+rec yards":  "rush+rec yards",
    # Hockey
    "shots":           "shots on goal",
    "shots on goal":   "shots on goal",
    "goals+assists":   "points",
    # Tennis
    "games won":       "games won",
    "sets won":        "sets won",
    # Esports
    "kills":           "kills",
    "deaths":          "deaths",
    "maps won":        "maps won",
}


def _normalize_stat(raw: str) -> str:
    """Return canonical stat name for a PrizePicks stat label."""
    return _STAT_NORM.get(raw.lower().strip(), raw.lower().strip())


def _parse_dt(value: Any) -> Optional[datetime]:
    """
    Parse a datetime value from either a datetime object or an ISO-8601 string.
    Returns None on failure — callers must handle None game_time gracefully.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── PrizePicksLine → PlayerProp adapter ──────────────────────────────────────

def pp_line_to_player_prop(pp_line: "PrizePicksLine") -> PlayerProp:
    """
    Convert a PrizePicksLine (bot/prizepicks.py) → normalized PlayerProp.

    This is the only place where PrizePicks-specific field names are known.
    The rest of the pipeline works exclusively with PlayerProp.
    """
    return PlayerProp(
        provider    = "PrizePicks",
        sport       = pp_line.sport,
        player_name = pp_line.player_name,
        team        = pp_line.team,
        stat_type   = _normalize_stat(pp_line.stat_type),
        line_value  = float(pp_line.line_value),
        game_time   = _parse_dt(getattr(pp_line, "start_time", None)),
        external_id = str(pp_line.external_id),
        game_id     = f"{pp_line.sport}:{pp_line.player_name}:{pp_line.stat_type}",
        fetched_at  = datetime.utcnow(),
    )


# ── PrizePicksProvider (live — currently disabled) ────────────────────────────

class PrizePicksProvider(PropProviderBase):
    """
    PrizePicks concrete provider.

    Wraps PrizePicksClient from bot/prizepicks.py.  Currently returns
    is_available() = False because DataDome blocks direct API access.

    To activate:
      1.  Implement auth/proxy bypass in PrizePicksClient.
      2.  Subclass or override is_available() to return True.
      3.  The rest of the system picks up PlayerProp objects automatically.
    """

    def __init__(
        self,
        *,
        sport_filter: Optional[list[str]] = None,
        session: Any = None,          # aiohttp.ClientSession — passed to client
    ) -> None:
        self._sport_filter = sport_filter
        self._session      = session

    # ── PropProviderBase interface ────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "PrizePicks"

    @property
    def sport_keys(self) -> list[str]:
        return self._sport_filter or [
            "MLB", "NBA", "NFL", "NHL", "WNBA",
            "SOCCER", "TENNIS", "CS", "DOTA", "LOL",
        ]

    def is_available(self) -> bool:
        """
        DataDome blocks direct API calls.  Returns False until a bypass exists.
        Override this method or use PrizePicksManualProvider for testing.
        """
        return False

    async def fetch_props(self) -> list[PlayerProp]:
        """
        Fetch live PrizePicks props.

        Not yet functional — DataDome protection prevents direct API calls.
        Use PrizePicksManualProvider to supply props from a test/manual feed.
        """
        if not _PRIZEPICKS_MODULE:
            raise ImportError(
                "bot/prizepicks.py could not be imported — cannot fetch PrizePicks lines."
            )
        raise NotImplementedError(
            "PrizePicks API is DataDome-protected and cannot be called directly. "
            "Use PrizePicksManualProvider to ingest data from a manual or test feed."
        )

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)


# ── PrizePicksManualProvider (manual / test feed) ─────────────────────────────

class PrizePicksManualProvider(PropProviderBase):
    """
    Manual / test-feed PrizePicks provider.

    Accepts a list of dicts (portable) or PrizePicksLine objects and serves
    them as normalized PlayerProp objects without any API dependency.

    This supports:
      * Manual import feeds (copy-paste / scrape)
      * Cached feeds written to disk
      * Test fixtures in automated tests
      * Future alternative data sources

    Usage — from raw dicts (JSON-friendly):

        provider = PrizePicksManualProvider.from_dicts([
            {
                "external_id": "pp-001",
                "player_name": "LeBron James",
                "team": "LAL",
                "sport": "NBA",
                "stat_type": "Points",
                "line_value": 25.5,
                "start_time": "2026-08-01T19:00:00",
            },
        ])
        props = await provider.fetch_props()

    Usage — from PrizePicksLine objects:

        lines = [PrizePicksLine(...), ...]
        provider = PrizePicksManualProvider(lines=lines)
        props = await provider.fetch_props()
    """

    def __init__(
        self,
        *,
        lines:         Optional[list[Any]]  = None,   # list[PrizePicksLine]
        raw_dicts:     Optional[list[dict]] = None,   # JSON-friendly records
        sport_filter:  Optional[list[str]]  = None,
    ) -> None:
        self._lines        = lines        or []
        self._raw_dicts    = raw_dicts    or []
        self._sport_filter = sport_filter

    @classmethod
    def from_dicts(
        cls,
        data: list[dict],
        sport_filter: Optional[list[str]] = None,
    ) -> "PrizePicksManualProvider":
        """Build a provider from a list of raw dict records."""
        return cls(raw_dicts=data, sport_filter=sport_filter)

    # ── PropProviderBase interface ────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "PrizePicks"

    @property
    def sport_keys(self) -> list[str]:
        return self._sport_filter or ["MLB", "NBA", "NFL", "NHL", "WNBA"]

    def is_available(self) -> bool:
        return bool(self._lines or self._raw_dicts)

    async def fetch_props(self) -> list[PlayerProp]:
        props: list[PlayerProp] = []
        now = datetime.utcnow()

        # Path 1 — PrizePicksLine objects
        for ln in self._lines:
            prop = pp_line_to_player_prop(ln)
            if self._sport_filter is None or prop.sport in self._sport_filter:
                props.append(prop)

        # Path 2 — raw dict records
        for d in self._raw_dicts:
            sport = d.get("sport", "")
            if self._sport_filter and sport not in self._sport_filter:
                continue

            try:
                line_value = float(d.get("line_value", 0.0))
            except (TypeError, ValueError):
                line_value = 0.0

            props.append(PlayerProp(
                provider    = "PrizePicks",
                sport       = sport,
                player_name = d.get("player_name", ""),
                team        = d.get("team", ""),
                stat_type   = _normalize_stat(d.get("stat_type", "")),
                line_value  = line_value,
                game_time   = _parse_dt(d.get("start_time")),
                external_id = str(d.get("external_id", "")),
                game_id     = d.get("game_id", ""),
                fetched_at  = now,
            ))

        return props

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)

    def __len__(self) -> int:
        return len(self._lines) + len(self._raw_dicts)

    def __repr__(self) -> str:
        return (
            f"<PrizePicksManualProvider "
            f"lines={len(self._lines)} dicts={len(self._raw_dicts)} "
            f"sports={self._sport_filter}>"
        )
