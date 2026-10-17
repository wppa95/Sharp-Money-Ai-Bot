"""Current source offerings, conservative comparisons, and traceable coverage.

No requests, scoring, or delivery ownership live here. Unknown markets remain
visible; lack of an exact comparison never disqualifies a standalone pick.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
from functools import wraps
import inspect
import math
import re
from typing import Any


def market_identity(sport: str, label: str) -> str:
    """Conservative, sport-aware aliases; unknown definitions are not guessed."""
    raw = re.sub(r"\s+", " ", str(label).lower().replace("_", " ")).strip()
    raw = re.sub(r"\s*\+\s*", "+", raw)
    aliases = {
        "MLB": {
            "hrr": "hits+runs+rbis", "hits+runs+rbi": "hits+runs+rbis",
            "hits+runs+rbis": "hits+runs+rbis", "batter hits": "hits",
            "batter walks": "walks", "hitter strikeouts": "batter strikeouts",
            "pitching strikeouts": "pitcher strikeouts",
            "pitcher ks": "pitcher strikeouts", "strikeouts (pitcher)": "pitcher strikeouts",
            "pitching outs": "outs recorded", "pitcher outs": "outs recorded",
            "pitcher earned runs": "earned runs allowed",
            "pitcher hits allowed": "hits allowed", "pitcher walks allowed": "walks allowed",
            "hr": "home runs", "rbi": "rbis",
        },
        "NHL": {
            "sog": "shots on goal", "shots": "shots on goal",
            "blocked shots": "blocked shots", "blocks": "blocked shots",
            "saves": "goalie saves", "goals+assists": "points",
        },
        "NBA": {"pts": "points", "reb": "rebounds", "ast": "assists"},
        "WNBA": {"pts": "points", "reb": "rebounds", "ast": "assists"},
    }
    return aliases.get(str(sport).upper(), {}).get(raw, raw)


def directions_from(value: Any) -> tuple[str, ...]:
    """Read explicit selections only. Empty means unknown, not both sides."""
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    found = set()
    for item in value:
        if isinstance(item, dict):
            item = item.get("choice") or item.get("direction") or item.get("side") or item.get("name")
        side = str(item or "").strip().upper()
        side = {"HIGHER": "OVER", "LOWER": "UNDER", "MORE": "OVER", "LESS": "UNDER"}.get(side, side)
        if side in {"OVER", "UNDER"}:
            found.add(side)
    return tuple(sorted(found))


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def offering_key(prop: Any) -> tuple:
    return (
        str(getattr(prop, "provider", "") or getattr(prop, "sportsbook", "")),
        str(getattr(prop, "sport", "")).upper(),
        str(getattr(prop, "player_name", "")).strip().casefold(),
        str(getattr(prop, "stat_type", "")).strip().casefold(),
        str(getattr(prop, "game_id", "") or getattr(prop, "event_id", "")),
        str(getattr(prop, "external_id", "")),
        getattr(prop, "line_value", None),
        str(getattr(prop, "line_variant", "")),
        directions_from(getattr(prop, "available_directions", ()) or getattr(prop, "direction", "")),
    )


class OfferingCoverage:
    """Bounded session inventory; retained stage sets count unique offerings."""

    def __init__(self) -> None:
        self.rows: dict[tuple, Any] = {}
        self.stages: dict[tuple, dict[str, str]] = {}
        self.observations: Counter = Counter()
        self.failures: Counter = Counter()
        self.index: dict[tuple, set] = {}
        self.raw_markets: Counter = Counter()
        self.raw_rejections: dict[tuple, str] = {}

    def raw(self, provider: str, sport: str, market: str, source_id: str,
            reason: str = "") -> None:
        self.raw_markets[(provider, sport or "UNKNOWN", market or "UNKNOWN")] += 1
        if reason:
            if len(self.raw_rejections) >= 5000:
                self.raw_rejections.pop(next(iter(self.raw_rejections)))
            self.raw_rejections[(provider, sport, market, source_id)] = reason

    @staticmethod
    def _identity(provider: str, player: str, sport: str, stat: str, line: float) -> tuple:
        return (provider.casefold(), player.strip().casefold(), sport.upper(),
                market_identity(sport, stat), line)

    def observe(self, prop: Any) -> None:
        now = datetime.utcnow()
        if len(self.rows) >= 50000:
            cutoff = now.timestamp() - 86400
            for key, row in list(self.rows.items()):
                stamp = _utc(getattr(row, "fetched_at", None))
                if stamp is None or stamp.timestamp() < cutoff:
                    self.rows.pop(key, None)
                    self.stages.pop(key, None)
                    self.observations.pop(key, None)
                    identity = self._identity(row.provider, row.player_name, row.sport, row.stat_type, row.line_value)
                    bucket = self.index.get(identity, set())
                    bucket.discard(key)
                    if not bucket:
                        self.index.pop(identity, None)
            if len(self.rows) >= 50000:
                oldest = next(iter(self.rows))
                removed = self.rows.pop(oldest)
                self.stages.pop(oldest, None)
                self.observations.pop(oldest, None)
                identity = self._identity(removed.provider, removed.player_name, removed.sport, removed.stat_type, removed.line_value)
                self.index.get(identity, set()).discard(oldest)
                if not self.index.get(identity):
                    self.index.pop(identity, None)
        key = offering_key(prop)
        previous = self.rows.get(key)
        self.rows[key] = prop
        self.index.setdefault(self._identity(
            prop.provider, prop.player_name, prop.sport, prop.stat_type, prop.line_value
        ), set()).add(key)
        if previous is None or getattr(previous, "fetched_at", None) != getattr(prop, "fetched_at", None):
            self.observations[key] += 1
        self.mark(prop, "NORMALIZED")

    def mark(self, prop: Any, stage: str, reason: str = "") -> None:
        key = offering_key(prop)
        if key in self.rows:
            self.stages.setdefault(key, {})[stage] = reason

    def evaluation(self, *, provider: str, player: str, sport: str, stat: str,
                   line: float, event_id: str, decision: Any, hit_rates: Any) -> None:
        for key in self.index.get(self._identity(provider, player, sport, stat, line), ()):
            row = self.rows.get(key)
            if row is None or (event_id and row.game_id and row.game_id != event_id):
                continue
            self.mark(row, "EVALUATION_ELIGIBLE")
            self.mark(row, "EVALUATED")
            if getattr(hit_rates, "has_real_data", False):
                self.mark(row, "HISTORY")
            else:
                self.mark(row, "EVIDENCE_INSUFFICIENT", "no completed game history")
            if getattr(decision, "recommendation", None) in {"OVER", "UNDER"}:
                self.mark(row, "DIRECTIONAL")
            else:
                self.mark(row, "REJECTED", str(getattr(decision, "reason", "") or "no direction"))

    def match(self, *, provider: str, player: str, sport: str, stat: str,
              line: float, event_id: str = "", game_time: datetime | None = None,
              opponent: str = "", source_id: str = "") -> Any | None:
        candidates = []
        for key in self.index.get(self._identity(provider, player, sport, stat, line), ()):
            row = self.rows.get(key)
            if row is None:
                continue
            if getattr(row, "removed", False):
                continue
            if str(row.provider).casefold() != provider.casefold():
                continue
            if str(row.player_name).strip().casefold() != player.strip().casefold():
                continue
            if str(row.sport).upper() != sport.upper() or market_identity(sport, row.stat_type) != market_identity(sport, stat):
                continue
            if row.line_value != line:
                continue
            if source_id and row.external_id != source_id:
                continue
            if event_id and str(row.game_id) != str(event_id):
                continue
            if game_time and _utc(row.game_time) != _utc(game_time):
                continue
            observed = _utc(row.fetched_at)
            if observed is None or not 0 <= (datetime.utcnow() - observed).total_seconds() <= 900:
                continue
            candidates.append(row)
        # Ambiguous events/variants must not be collapsed to a guessed offering.
        return candidates[0] if len(candidates) == 1 else None

    def simultaneous_alternatives(
        self, *, provider: str, player: str, sport: str, stat: str,
        line_a: float, source_a: str, event_a: str,
        line_b: float, source_b: str, event_b: str,
    ) -> bool:
        """Distinguish concurrent alt offerings from a rotating/moving quote."""
        if not source_a or not source_b:
            return False
        a = self.match(provider=provider, player=player, sport=sport, stat=stat,
                       line=line_a, source_id=source_a, event_id=event_a)
        b = self.match(provider=provider, player=player, sport=sport, stat=stat,
                       line=line_b, source_id=source_b, event_id=event_b)
        return bool(
            a is not None and b is not None
            and a.fetched_at == b.fetched_at
            and a.game_id == b.game_id
            and (a.line_value != b.line_value or a.line_variant != b.line_variant)
        )

    def comparisons(self, pick: Any) -> list[Any]:
        result = []
        if not getattr(pick, "available_directions", ()):
            return result
        start = _utc(getattr(pick, "game_time", None))
        opponent = str(getattr(pick, "opponent", "") or "").strip().casefold()
        def participants(prop):
            return frozenset(
                str(name).strip().casefold()
                for _, name in getattr(prop, "event_teams", ())
                if str(name).strip()
            )
        pick_teams = participants(pick)
        for row in self.rows.values():
            if getattr(row, "removed", False):
                continue
            if row.provider == pick.provider or {row.provider, pick.provider} != {"Underdog", "PrizePicks"}:
                continue
            if row.sport != pick.sport or row.player_name.strip().casefold() != pick.player_name.strip().casefold():
                continue
            if market_identity(row.sport, row.original_stat_type or row.stat_type) != market_identity(pick.sport, pick.original_stat_type or pick.stat_type):
                continue
            # Provider event IDs are not universal. Accept either the same
            # explicit ID plus start, or exact start plus confirmed opponent.
            if start is None or _utc(row.game_time) != start:
                continue
            same_event = bool(row.game_id and row.game_id == pick.game_id)
            other_opponent = str(getattr(row, "opponent", "") or "").strip().casefold()
            same_teams = len(pick_teams) == 2 and participants(row) == pick_teams
            if not same_event and not same_teams and not (opponent and other_opponent == opponent):
                continue
            # Unknown periods/rules are never equated across different labels;
            # the conservative alias table above only contains known definitions.
            if not getattr(row, "available_directions", ()):
                continue
            observed = _utc(row.fetched_at)
            if observed is None or not 0 <= (datetime.utcnow() - observed).total_seconds() <= 900:
                continue
            if math.isfinite(row.line_value):
                result.append(row)
        return sorted(result, key=lambda row: (row.line_variant, row.line_value, row.external_id))

    def report(self, sport: str = "", platform: str = "") -> list[str]:
        from engine.sport_policy import canonical_sport_key
        sport = canonical_sport_key(sport)
        groups: dict[tuple, list] = {}
        for key, row in self.rows.items():
            if sport and canonical_sport_key(row.sport) != sport:
                continue
            if platform and row.provider.casefold() != platform.casefold():
                continue
            groups.setdefault((row.provider, row.sport, row.stat_type), []).append(key)
        lines = ["<b>MARKET INVENTORY — current session</b>",
                 "Observed coverage only; absent markets and earlier stages remain unknown.",
                 "Stages count unique offerings reaching each stage during this session.",
                 "Use /funnel markets SPORT [Underdog|PrizePicks] [page] or /funnel offering SOURCE_ID."]
        for (provider, league, market), keys in sorted(groups.items()):
            stages = Counter(stage for key in keys for stage in self.stages.get(key, {}))
            reasons = Counter(reason for key in keys for reason in self.stages.get(key, {}).values() if reason)
            lines.append(
                f"{escape(provider)} · {escape(league)} · {escape(market)}: "
                f"{len(keys)} unique / {sum(self.observations[key] for key in keys)} observations; "
                + ", ".join(f"{stage}={count}" for stage, count in sorted(stages.items()))
                + (f"; reasons: {escape(str(dict(reasons)))}" if reasons else "")
            )
        for (provider, reason), count in sorted(self.failures.items()):
            lines.append(f"{escape(provider)} normalization rejected: {escape(reason)}={count}")
        for (provider, league, market), count in sorted(self.raw_markets.items()):
            if sport and canonical_sport_key(league) != sport or platform and provider.casefold() != platform.casefold():
                continue
            lines.append(f"RAW {escape(provider)} · {escape(league)} · {escape(market)}: {count} observations")
        for (provider, league, market, source_id), reason in self.raw_rejections.items():
            if sport and league.upper() != sport.upper() or platform and provider.casefold() != platform.casefold():
                continue
            lines.append(f"REJECTED {escape(provider)} · {escape(league)} · {escape(market)} · {escape(source_id)}: {escape(reason)}")
        if not groups:
            lines.append("No observations in this session for the requested scope. Coverage unknown.")
        return lines

    def detail(self, source_id: str) -> list[str]:
        lines = ["<b>OFFERING TRACE — current session</b>"]
        for key, row in self.rows.items():
            if row.external_id != source_id:
                continue
            lines.extend([
                escape(f"{row.provider} · {row.sport} · {row.player_name}"),
                escape(f"{row.stat_type} · line {row.line_value:g} · {row.line_variant or 'unclassified'}"),
                escape(f"Event: {row.game_id} · {row.event or 'matchup unavailable'}"),
                escape(f"Directions: {', '.join(row.available_directions) or 'not supplied'}"),
                escape(f"Observed: {row.fetched_at} UTC"),
                escape(str(self.stages.get(key, {"pending": "not yet evaluated"}))),
            ])
        for (provider, sport, market, identity), reason in self.raw_rejections.items():
            if identity == source_id:
                lines.append(escape(f"{provider} · {sport} · {market}: NORMALIZATION_REJECTED — {reason}"))
        if len(lines) == 1:
            lines.append("No matching source identity in this session; coverage unknown.")
        return lines


coverage = OfferingCoverage()


def trace_delivery(function):
    """Capture every existing delivery return without changing any gate."""
    signature = inspect.signature(function)

    @wraps(function)
    async def traced(*args, **kwargs):
        values = signature.bind_partial(*args, **kwargs)
        values.apply_defaults()
        params = values.arguments
        row = coverage.match(
            provider=params.get("source", "Underdog"),
            player=params.get("player_name", ""), sport=params.get("sport", ""),
            stat=params.get("stat_type", ""), line=params.get("new_line", 0),
            source_id=str(params.get("source_id") or ""),
            game_time=params.get("game_time"),
        )
        result = await function(*args, **kwargs)
        if row is not None and not getattr(result, "sent", False):
            reason = (
                getattr(result, "filtered_reason", None)
                or ("duplicate alert" if getattr(result, "deduped", False) else "delivery not confirmed")
            )
            coverage.mark(row, "DELIVERY_BLOCKED", reason)
        return result
    return traced
