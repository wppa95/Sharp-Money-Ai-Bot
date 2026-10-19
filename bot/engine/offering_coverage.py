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
        self._ready_scopes: set[tuple[str, str]] = set()

    @staticmethod
    def _scope(provider: str, sport: str = "") -> tuple[str, str]:
        from engine.sport_policy import player_sport_key

        return (
            str(provider or "").strip().casefold(),
            player_sport_key(sport) if sport else "",
        )

    def reset_readiness(self) -> None:
        """Start a new runtime session with no provider snapshots trusted."""
        self._ready_scopes.clear()

    def mark_scope_ready(self, provider: str, sport: str = "") -> None:
        """Mark a complete persisted source snapshot available for delivery."""
        scope = self._scope(provider, sport)
        if scope[0]:
            self._ready_scopes.add(scope)

    def is_scope_ready(self, provider: str, sport: str = "") -> bool:
        provider_key, sport_key = self._scope(provider, sport)
        return (
            (provider_key, "") in self._ready_scopes
            or (provider_key, sport_key) in self._ready_scopes
        )

    def ready_scopes(self) -> frozenset[tuple[str, str]]:
        """Return provider-wide and provider/sport snapshots ready for claims."""
        return frozenset(self._ready_scopes)

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

    def clear_scope(self, provider: str, sport: str = "") -> None:
        """Discard the prior in-memory offering snapshot for a successful feed."""
        from engine.sport_policy import player_sport_key

        provider_key = str(provider or "").casefold()
        sport_key = player_sport_key(sport) if sport else ""
        if sport_key:
            self._ready_scopes.discard((provider_key, sport_key))
        else:
            self._ready_scopes = {
                scope for scope in self._ready_scopes if scope[0] != provider_key
            }
        for key, row in list(self.rows.items()):
            if str(getattr(row, "provider", "")).casefold() != provider_key:
                continue
            if sport_key and player_sport_key(getattr(row, "sport", "")) != sport_key:
                continue
            self.rows.pop(key, None)
            self.stages.pop(key, None)
            self.observations.pop(key, None)
            identity = self._identity(
                row.provider, row.player_name, row.sport, row.stat_type,
                row.line_value,
            )
            bucket = self.index.get(identity)
            if bucket is not None:
                bucket.discard(key)
                if not bucket:
                    self.index.pop(identity, None)

    def mark(self, prop: Any, stage: str, reason: str = "") -> None:
        key = offering_key(prop)
        if key in self.rows:
            self.stages.setdefault(key, {})[stage] = reason

    def validate_selection(
        self,
        *,
        provider: str,
        source_id: str,
        event_id: str,
        player: str,
        sport: str,
        stat: str,
        line: float,
        direction: str,
        line_variant: str | None,
        game_time: datetime | None,
        max_age_seconds: int = 900,
    ) -> tuple[Any | None, str]:
        """Fail closed unless the exact, fresh source offering supports a pick."""
        from engine.sport_policy import player_sport_key

        sport_key = player_sport_key(sport)
        direction = str(direction or "").strip().upper()
        if direction not in {"OVER", "UNDER"}:
            return None, "DIRECTION_UNVERIFIED"
        if not all(
            str(value or "").strip()
            for value in (provider, source_id, event_id, player, sport, stat)
        ) or game_time is None or line_variant is None and provider.casefold() == "prizepicks":
            return None, "EXACT_OFFERING_IDENTITY_INCOMPLETE"
        try:
            line = float(line)
        except (TypeError, ValueError):
            return None, "EXACT_OFFERING_IDENTITY_INCOMPLETE"
        if not math.isfinite(line):
            return None, "EXACT_OFFERING_IDENTITY_INCOMPLETE"

        same_source = []
        for key in self.index.get(
            self._identity(provider, player, sport_key, stat, line), ()
        ):
            row = self.rows.get(key)
            if row is None:
                continue
            if str(getattr(row, "provider", "")).casefold() != provider.casefold():
                continue
            if str(getattr(row, "external_id", "") or "").strip() != str(source_id).strip():
                continue
            if str(getattr(row, "game_id", "") or "").strip() != str(event_id).strip():
                continue
            if str(getattr(row, "player_name", "") or "").strip().casefold() != player.strip().casefold():
                continue
            if player_sport_key(getattr(row, "sport", "")) != sport_key:
                continue
            if market_identity(sport_key, str(getattr(row, "stat_type", "") or "")) != market_identity(sport_key, stat):
                continue
            if _utc(getattr(row, "game_time", None)) != _utc(game_time):
                continue
            if getattr(row, "line_value", None) == line:
                same_source.append(row)

        exact_line = same_source
        if not exact_line:
            return None, "EXACT_OFFERING_UNVERIFIED"

        expected_variant = str(line_variant or "").strip().casefold()
        if line_variant is not None:
            exact_line = [
                row for row in exact_line
                if str(getattr(row, "line_variant", "") or "").strip().casefold()
                == expected_variant
            ]
        if not exact_line:
            return None, "LINE_VARIANT_NOT_OFFERED"
        if len(exact_line) != 1:
            return None, "EXACT_OFFERING_AMBIGUOUS"

        row = exact_line[0]
        if getattr(row, "removed", False):
            return None, "OFFERING_REMOVED"
        observed = _utc(getattr(row, "fetched_at", None))
        age = (
            (datetime.utcnow() - observed).total_seconds()
            if observed is not None
            else None
        )
        if age is None or age < 0 or age > max_age_seconds:
            return None, "OFFERING_STALE"
        available = directions_from(
            getattr(row, "available_directions", ())
            or getattr(row, "direction", "")
        )
        if not available:
            return None, "OFFERING_DIRECTIONS_UNVERIFIED"
        if direction not in available:
            return None, "DIRECTION_NOT_OFFERED"
        return row, ""

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
        from engine.sport_policy import player_sport_key

        start = _utc(getattr(pick, "game_time", None))
        if start is None:
            return result

        def _team_names(prop: Any) -> frozenset[str]:
            names = {
                " ".join(str(name).split()).casefold()
                for _, name in (getattr(prop, "event_teams", ()) or ())
                if str(name or "").strip()
            }
            if len(names) == 2:
                return frozenset(names)
            team = " ".join(str(getattr(prop, "team", "") or "").split()).casefold()
            opponent = " ".join(
                str(getattr(prop, "opponent", "") or "").split()
            ).casefold()
            return frozenset((team, opponent)) if team and opponent else frozenset()

        pick_teams = _team_names(pick)
        pick_event_id = str(getattr(pick, "game_id", "") or "").strip()
        for row in self.rows.values():
            if getattr(row, "removed", False):
                continue
            if row.provider == pick.provider or {row.provider, pick.provider} != {"Underdog", "PrizePicks"}:
                continue
            if (
                player_sport_key(row.sport) != player_sport_key(pick.sport)
                or row.player_name.strip().casefold()
                != pick.player_name.strip().casefold()
            ):
                continue
            if market_identity(row.sport, row.original_stat_type or row.stat_type) != market_identity(pick.sport, pick.original_stat_type or pick.stat_type):
                continue
            if _utc(row.game_time) != start:
                continue
            row_teams = _team_names(row)
            # Reject contradictory verified teams even if provider IDs happen
            # to collide. When IDs differ across providers, exact team identity
            # plus the exact event start is the required cross-source join.
            if pick_teams and row_teams and pick_teams != row_teams:
                continue
            same_teams = bool(pick_teams and row_teams and pick_teams == row_teams)
            same_event_id = bool(
                pick_event_id
                and str(getattr(row, "game_id", "") or "").strip()
                == pick_event_id
            )
            if not same_teams and not same_event_id:
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


def _candidate_source(candidate: dict) -> tuple[Any, str, str, str, str, str, str, str]:
    prop = candidate.get("prop") or candidate.get("snap")
    provider = str(
        candidate.get("provider")
        or candidate.get("source")
        or getattr(prop, "provider", "")
        or getattr(prop, "sportsbook", "")
    ).strip()
    event_id = str(
        candidate.get("event_id")
        or getattr(prop, "game_id", "")
        or getattr(prop, "event_id", "")
        or getattr(prop, "match_id", "")
        or getattr(prop, "event", "")
        or ""
    ).strip()
    player = str(
        candidate.get("player")
        or candidate.get("player_name")
        or getattr(prop, "player_name", "")
        or getattr(prop, "player", "")
    ).strip()
    sport = str(candidate.get("sport") or getattr(prop, "sport", "") or "").upper()
    stat = str(
        candidate.get("stat")
        or candidate.get("stat_type")
        or getattr(prop, "stat_type", "")
        or ""
    ).strip()
    direction = str(
        getattr(candidate.get("decision"), "recommendation", "")
        or candidate.get("direction")
        or ""
    ).upper()
    variant = str(getattr(prop, "line_variant", "") or "").strip().casefold()
    return prop, provider, event_id, player, sport, stat, direction, variant


def _line_variant_kind(value: str) -> str:
    tokens = set(re.findall(r"[a-z]+", str(value or "").casefold()))
    if tokens & {"goblin", "demon"}:
        return "special"
    if tokens & {"standard", "normal", "regular"}:
        return "standard"
    return "unclassified"


def prioritize_standard_line_candidates(
    candidates: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Prefer an eligible standard line over its same-source special variant.

    The scope is intentionally provider-, event-, player-, sport-, market-, and
    direction-specific. Cross-platform observations never displace one
    platform's own candidate or establish its qualification.
    """
    standard_scopes = set()
    parsed = []
    for candidate in candidates:
        prop, provider, event_id, player, sport, stat, direction, variant = (
            _candidate_source(candidate)
        )
        scope = (
            provider.casefold(),
            event_id,
            player.casefold(),
            sport,
            market_identity(sport, stat),
            direction,
        )
        kind = _line_variant_kind(variant)
        if event_id and kind == "standard":
            standard_scopes.add(scope)
        parsed.append((candidate, scope, kind))

    kept, secondary = [], []
    for candidate, scope, kind in parsed:
        if kind == "special" and scope in standard_scopes:
            secondary.append(candidate)
        else:
            kept.append(candidate)
    return kept, secondary


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
