"""Conservative cross-provider market confirmation helpers.

This module deliberately contains no I/O, scoring, persistence, or delivery
logic.  It turns source-specific observations into a small common shape and
matches them to an Underdog target only when the market identity is safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional


_KNOWN_BOOKS = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
}

_PLAYER_MARKET_TO_STAT = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "3-pointers made",
    "player_steals": "steals",
    "player_blocks": "blocks",
    "player_hits": "hits",
    "player_pitcher_strikeouts": "pitcher strikeouts",
    "player_total_bases": "total bases",
}

_STAT_ALIASES = {
    "pts": "points",
    "points": "points",
    "reb": "rebounds",
    "rebounds": "rebounds",
    "ast": "assists",
    "assists": "assists",
    "3pm": "3-pointers made",
    "3-pointers made": "3-pointers made",
    "three-pointers made": "3-pointers made",
    "player threes": "3-pointers made",
    "hits": "hits",
    "pitcher strikeouts": "pitcher strikeouts",
    "strikeouts": "pitcher strikeouts",
    "total bases": "total bases",
}

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_EVENT_SEP_RE = re.compile(r"\s+(?:at|vs?|versus)\s+", re.IGNORECASE)


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _clean(value: Any) -> str:
    # Enum values are used by the existing analysis models.  Prefer their
    # public value (e.g. "Moneyline") over str(Enum), which is often
    # "MarketType.MONEYLINE".
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        value = value.value
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def normalize_book(value: Any) -> str:
    """Return a stable book key for DraftKings/FanDuel matching."""
    return _PUNCT_RE.sub("", _clean(value).lower())


def normalize_name(value: Any) -> str:
    """Normalize a player/team label without guessing at aliases."""
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", _clean(value).lower())).strip()


def normalize_stat(value: Any) -> str:
    # Composite player-prop labels may differ only by spacing around the
    # separator across providers (for example, "Points + Assists" versus
    # "points+assists"). Treat those as the same market key.
    raw = re.sub(r"\s*\+\s*", "+", _clean(value).lower())
    if raw in _PLAYER_MARKET_TO_STAT:
        raw = _PLAYER_MARKET_TO_STAT[raw]
    return _STAT_ALIASES.get(raw, raw)


def normalize_market(value: Any) -> str:
    raw = _clean(value).lower()
    if raw in {"h2h", "ml", "moneyline", "money line"}:
        return "moneyline"
    if raw in {"total", "totals", "total (o/u)", "over_under", "game total"}:
        return "total"
    if raw.startswith("player_") or "player prop" in raw:
        return "player_prop"
    return raw


def normalize_direction(value: Any) -> str:
    raw = _clean(value).lower()
    if raw in {"over", "o"}:
        return "OVER"
    if raw in {"under", "u"}:
        return "UNDER"
    return raw.upper()


def normalize_event(value: Any) -> str:
    """Normalize human-readable event labels for exact token-set comparison."""
    raw = _clean(value).lower()
    if not raw:
        return ""
    raw = _EVENT_SEP_RE.sub(" ", raw)
    tokens = [token for token in _PUNCT_RE.sub(" ", raw).split() if token]
    return " ".join(sorted(tokens))


def _has_human_event(value: Any) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return False
    return bool("@" in raw or re.search(r"\b(?:at|vs?|versus)\b", raw))


@dataclass(frozen=True)
class ConfirmationTarget:
    """The Underdog market to corroborate."""

    sport: str
    market_type: str
    selection: str = ""
    player: str = ""
    team: str = ""
    stat_type: str = ""
    direction: str = ""
    line: Optional[float] = None
    event: str = ""
    game_time: Optional[datetime] = None
    event_id: str = ""
    source_id: str = ""
    player_id: str = ""

    @property
    def market(self) -> str:
        return normalize_market(self.market_type)


@dataclass(frozen=True)
class ConfirmationObservation:
    """A source observation adapted from OddsLine, PlayerPropLine, or a row."""

    provider: str
    sport: str
    market_type: str
    selection: str = ""
    player: str = ""
    team: str = ""
    stat_type: str = ""
    direction: str = ""
    line: Optional[float] = None
    odds: Optional[int] = None
    event: str = ""
    observed_at: Optional[datetime] = None
    event_id: str = ""
    source_id: str = ""
    player_id: str = ""
    supplemental_source: str = ""
    game_time: Optional[datetime] = None

    @property
    def market(self) -> str:
        return normalize_market(self.market_type)

    @property
    def provider_key(self) -> str:
        return normalize_book(self.provider)


def observation_from_record(
    record: Any,
    *,
    provider: Optional[str] = None,
    sport: Optional[str] = None,
    market_type: Optional[str] = None,
    player: Optional[str] = None,
    stat_type: Optional[str] = None,
    direction: Optional[str] = None,
    line: Optional[float] = None,
    odds: Optional[int] = None,
    supplemental_source: Optional[str] = None,
) -> ConfirmationObservation:
    """Adapt one source record without mutating or depending on its class."""
    raw_market = market_type or _value(record, "market_type", "")
    raw_market_key = _value(record, "market_key", "")
    if raw_market_key:
        raw_market = raw_market_key

    raw_line = line if line is not None else _value(record, "line", None)
    try:
        parsed_line = float(raw_line) if raw_line is not None else None
    except (TypeError, ValueError):
        parsed_line = None

    raw_odds = odds if odds is not None else (
        _value(record, "american_odds", _value(record, "odds", None))
    )
    try:
        parsed_odds = int(raw_odds) if raw_odds is not None else None
    except (TypeError, ValueError):
        parsed_odds = None

    observed = (
        _value(record, "observed_at", None)
        or _value(record, "source_timestamp", None)
        or _value(record, "timestamp", None)
        or _value(record, "recorded_at", None)
        or _value(record, "fetched_at", None)
    )
    if not isinstance(observed, datetime):
        observed = None

    normalized_market = normalize_market(raw_market)
    raw_direction = (
        direction or _value(record, "description", "")
        or _value(record, "direction", "")
    )
    # OddsRecord/OddsLine game markets store the side in selection rather
    # than a separate description.  Do not infer a player-prop direction from
    # an arbitrary selection string.
    if not raw_direction and normalized_market == "total":
        raw_direction = _value(record, "selection", "")

    return ConfirmationObservation(
        provider=_clean(provider or _value(record, "sportsbook", _value(record, "provider", ""))),
        sport=_clean(sport if sport is not None else _value(record, "sport", "")).upper(),
        market_type=_clean(raw_market),
        selection=_clean(_value(record, "selection", "")),
        player=_clean(player or _value(record, "player_name", _value(record, "player", ""))),
        team=_clean(_value(record, "team", "")),
        stat_type=_clean(stat_type or _value(record, "stat_type", raw_market_key)),
        direction=normalize_direction(raw_direction),
        line=parsed_line,
        odds=parsed_odds,
        event=_clean(_value(record, "event", "")),
        observed_at=observed,
        event_id=_clean(_value(record, "event_id", _value(record, "game_id", ""))),
        source_id=_clean(_value(record, "source_id", _value(record, "external_id", ""))),
        player_id=_clean(_value(record, "player_id", _value(record, "athlete_id", ""))),
        supplemental_source=_clean(
            supplemental_source
            if supplemental_source is not None
            else _value(record, "supplemental_source", "")
        ),
        game_time=(
            _value(record, "game_time", _value(record, "start_time", None))
            if isinstance(
                _value(record, "game_time", _value(record, "start_time", None)),
                datetime,
            )
            else None
        ),
    )


def _same_sport(target: ConfirmationTarget, observation: ConfirmationObservation) -> bool:
    return _clean(target.sport).upper() == _clean(observation.sport).upper()


def _same_event(target: ConfirmationTarget, observation: ConfirmationObservation) -> bool:
    if target.event_id and observation.event_id:
        return target.event_id == observation.event_id
    # PropLine is explicitly used as cross-provider evidence.  Its opaque
    # event IDs must not be treated as a wildcard when the Underdog target
    # lacks the same provider-native ID.
    if observation.supplemental_source == "PropLine":
        if (
            target.game_time is not None
            and observation.game_time is not None
            and abs((target.game_time - observation.game_time).total_seconds()) <= 15 * 60
        ):
            return True
        if _has_human_event(target.event) and _has_human_event(observation.event):
            return normalize_event(target.event) == normalize_event(observation.event)
        return False
    # If either side has an event identity, do not treat a missing counterpart
    # as a wildcard.  Legacy sportsbook confirmations retain the established
    # permissive behavior below because their event IDs and display labels
    # often come from different namespaces.
    if target.event_id or observation.event_id:
        if not (_has_human_event(target.event) and _has_human_event(observation.event)):
            return True
        return normalize_event(target.event) == normalize_event(observation.event)
    # When both are human labels, require the complete normalized token set.
    if not (_has_human_event(target.event) and _has_human_event(observation.event)):
        return True
    return normalize_event(target.event) == normalize_event(observation.event)


def _same_team(target: ConfirmationTarget, observation: ConfirmationObservation) -> bool:
    target_team = normalize_name(target.team or target.selection)
    source_team = normalize_name(observation.team or observation.selection)
    return bool(target_team and source_team and target_team == source_team)


def _same_player(target_name: str, source_name: str, *, candidate_names: set[str]) -> bool:
    wanted = normalize_name(target_name)
    found = normalize_name(source_name)
    if not wanted or not found:
        return False
    if wanted == found:
        return True
    # Surname-only provider labels are accepted only when unambiguous.
    wanted_parts = wanted.split()
    found_parts = found.split()
    if len(wanted_parts) == 1 and found_parts[-1] == wanted:
        return len({name for name in candidate_names if name.split()[-1] == wanted}) == 1
    if len(found_parts) == 1 and wanted_parts[-1] == found:
        return len({name for name in candidate_names if name.split()[-1] == found}) == 1
    return False


def _matches_identity(
    target: ConfirmationTarget,
    observation: ConfirmationObservation,
    *,
    candidate_names: set[str],
) -> bool:
    if not _same_sport(target, observation) or target.market != observation.market:
        return False
    if not _same_event(target, observation):
        return False

    if target.market == "player_prop":
        if target.player_id and observation.player_id:
            if target.player_id != observation.player_id:
                return False
        if not _same_player(target.player, observation.player, candidate_names=candidate_names):
            return False
        if normalize_stat(target.stat_type) != normalize_stat(observation.stat_type):
            return False
        if target.direction and (
            normalize_direction(target.direction)
            != normalize_direction(observation.direction)
        ):
            return False
        # Team IDs and provider-specific team codes are often not comparable.
        # Only reject a disagreement when both sources expose the same kind of
        # human-readable team label.
        if target.team and observation.team:
            target_team = normalize_name(target.team)
            source_team = normalize_name(observation.team)
            if target_team == source_team:
                pass
            elif len(target_team.split()) > 1 and len(source_team.split()) > 1:
                return False
        return True

    if target.market == "moneyline":
        return _same_team(target, observation)

    if target.market == "total":
        return normalize_direction(target.direction) == normalize_direction(observation.direction)

    return False


def confirm_market(
    target: ConfirmationTarget,
    observations: Iterable[ConfirmationObservation],
    *,
    allowed_providers: Iterable[str] = ("draftkings", "fanduel"),
    max_age_seconds: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Return a serializable confirmation summary, or ``None`` on no match.

    The result is informational.  ``confirmed`` means at least one directional
    source observation exists; ``strong_confirmation`` retains the established
    two-book/half-unit convention.  It never asserts that absent data exists.
    """
    allowed = {normalize_book(book) for book in allowed_providers}
    current_time = now or datetime.utcnow()
    source_rows = [
        row for row in observations
        if row.provider_key in allowed and _same_sport(target, row)
    ]
    candidate_names = {
        normalize_name(row.player)
        for row in source_rows
        if normalize_name(row.player)
    }
    matches = [
        row for row in source_rows
        if _matches_identity(target, row, candidate_names=candidate_names)
    ]

    if max_age_seconds is not None:
        fresh: list[ConfirmationObservation] = []
        for row in matches:
            if row.observed_at is None:
                fresh.append(row)
                continue
            age = (current_time - row.observed_at).total_seconds()
            if 0 <= age <= max_age_seconds:
                fresh.append(row)
        matches = fresh

    if not matches:
        return None

    # PropLine is a single authorized source rather than a collection of
    # sportsbooks.  If it gives multiple event/line/direction candidates for
    # the same target, do not choose one arbitrarily.
    pp_matches = [row for row in matches if row.provider_key == "prizepicks"]
    if pp_matches:
        pp_signatures = {
            (
                row.event_id or normalize_event(row.event),
                row.line,
                normalize_direction(row.direction),
            )
            for row in pp_matches
        }
        if len(pp_signatures) > 1:
            return None

    # One source can emit duplicate rows. Keep the newest row per provider;
    # ambiguity between different providers remains visible.
    by_provider: dict[str, ConfirmationObservation] = {}
    for row in matches:
        existing = by_provider.get(row.provider_key)
        if existing is None or (
            row.observed_at is not None
            and (existing.observed_at is None or row.observed_at >= existing.observed_at)
        ):
            by_provider[row.provider_key] = row

    unique = list(by_provider.values())
    lines = [row.line for row in unique if row.line is not None]
    odds = [row.odds for row in unique if row.odds not in (None, 0)]
    avg_line = sum(lines) / len(lines) if lines else None
    avg_odds = round(sum(odds) / len(odds)) if odds else None
    names = [_KNOWN_BOOKS.get(row.provider_key, row.provider) for row in unique]
    line_delta = (
        abs(avg_line - target.line)
        if avg_line is not None and target.line is not None
        else None
    )
    strong = len(lines) >= 2 and line_delta is not None and line_delta <= 0.5

    return {
        "market_type": target.market,
        "num_books": len(unique),
        "matched_books": sorted(names),
        "directional_books": len(lines),
        "avg_line": avg_line,
        "avg_odds": avg_odds,
        "line_delta": line_delta,
        "confirmed": bool(lines or odds),
        "strong_confirmation": strong,
        "provenance": {
            "provider": (
                "mixed"
                if len({row.provider for row in unique}) > 1
                else (unique[0].provider if unique else "")
            ),
            "supplemental_source": (
                sorted(
                    {
                        row.supplemental_source
                        for row in unique
                        if row.supplemental_source
                    }
                )
            ),
            "event_ids": sorted({row.event_id for row in unique if row.event_id}),
            "player_ids": sorted({row.player_id for row in unique if row.player_id}),
        },
        "source": (
            "PrizePicks"
            if unique and all(row.provider_key == "prizepicks" for row in unique)
            else "OddsAPI"
        ),
        "notes": (
            f"{len(unique)} book{'s' if len(unique) != 1 else ''}"
            + (f" · avg {avg_line:.1f}" if avg_line is not None else " · no line")
        ),
    }


__all__ = [
    "ConfirmationObservation",
    "ConfirmationTarget",
    "confirm_market",
    "normalize_book",
    "normalize_direction",
    "normalize_event",
    "normalize_market",
    "normalize_name",
    "normalize_stat",
    "observation_from_record",
]