"""
pregame_watch.py — Pregame Market Watch (Continuous, All-Day).

Continuously monitors upcoming games across all supported sports, tracking
opening lines and surfacing valuable player prop opportunities before the
market moves, lines get worse, or props disappear.

Activated: continuous scheduler job runs every PREGAME_SCAN_INTERVAL seconds.

Workflow:
  Each cycle:
    - Scan for new props and record opening lines (morning_scan)
    - Re-fetch current lines, compute movement (pregame_scan)
    - Alert on first-detection of qualifying props (conf ≥ 60)
    - Re-alert when significant movement occurs on tracked props
    - Clear stale entries for games that have started

  Alert format:  🟣 PREGAME PLAYER PROP OPPORTUNITY
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# Minimum line movement (absolute) to flag as meaningful
MOVEMENT_THRESHOLD = 0.5

# How many hours before game start to run the pre-game scan
PREGAME_SCAN_HOURS = 3.0

# How many hours before game start to treat a prop as upcoming (morning scan window)
UPCOMING_WINDOW_HOURS = 16.0

# Minimum number of providers with a line to include in watch
MIN_PROVIDERS_TO_WATCH = 1


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class WatchLine:
    """A single provider's line at a specific point in time."""
    provider:   str
    emoji:      str
    line_value: float
    fetched_at: datetime


@dataclass
class PregameWatchEntry:
    """
    All available data for one player × stat combination in one upcoming game.

    Created during the morning scan, updated during the pre-game scan.
    Cleared when game_start passes.
    """
    player_name:   str
    sport:         str
    stat_type:     str
    game_id:       Optional[str]
    game_start:    Optional[datetime]

    # Opening lines recorded at morning-scan time
    opening_lines: dict[str, WatchLine] = field(default_factory=dict)

    # Current lines recorded at pre-game-scan time (empty until scan runs)
    current_lines: dict[str, WatchLine] = field(default_factory=dict)

    # Computed movement per provider (current - opening)
    movement:      dict[str, float]     = field(default_factory=dict)

    created_at:    Optional[datetime]   = None
    updated_at:    Optional[datetime]   = None

    @property
    def has_movement(self) -> bool:
        return any(abs(m) >= MOVEMENT_THRESHOLD for m in self.movement.values())

    @property
    def best_current_line(self) -> Optional[tuple[str, float]]:
        """Return (provider, line) for the best available current line."""
        _order = ["PrizePicks", "Underdog", "DraftKings", "FanDuel"]
        for p in _order:
            if p in self.current_lines:
                return (p, self.current_lines[p].line_value)
        return None

    @property
    def provider_count(self) -> int:
        """Number of providers with current lines."""
        return len(self.current_lines)

    def movement_summary(self) -> str:
        """Human-readable summary of all movement."""
        parts = []
        for provider, delta in self.movement.items():
            if abs(delta) >= MOVEMENT_THRESHOLD:
                sign  = "+" if delta > 0 else ""
                parts.append(f"{provider}: {sign}{delta:.1f}")
        return "  ".join(parts) if parts else "no significant movement"

    def minutes_to_game(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.game_start is None:
            return None
        _now = now or datetime.utcnow()
        delta = (self.game_start - _now).total_seconds() / 60.0
        return delta


# ── Alert formatter ───────────────────────────────────────────────────────────

def format_pregame_watch_alert(
    entry: PregameWatchEntry,
    comp: Optional[Any] = None,
) -> str:
    """
    Format a 🟣 PREGAME PLAYER PROP OPPORTUNITY alert for Telegram (HTML).

    When a PlayerPropMarketComparison is provided, shows the full 4-provider
    market view (Available Lines, Movement, Best Available Line, Market Quality,
    Confidence, Reason).  Falls back to opening/current line comparison when
    comp is None.
    """
    sport_icons = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾", "NHL": "🏒",
        "UFC": "🥊", "WNBA": "🏀", "SOCCER": "⚽", "TENNIS": "🎾",
        "CS": "🖥️", "DOTA": "🎮", "LOL": "🎮",
        "TABLE TENNIS": "🏓", "BADMINTON": "🏸",
    }
    s_icon = sport_icons.get(entry.sport.upper(), "🎯")
    div = "─" * 16

    mins = entry.minutes_to_game()
    if mins is not None and mins >= 0:
        if mins < 60:
            time_str = f"{int(mins)} min"
        else:
            h, m = int(mins // 60), int(mins % 60)
            time_str = f"{h}h {m}m" if m else f"{h}h"
    elif entry.game_start:
        time_str = entry.game_start.strftime("%H:%M UTC")
    else:
        time_str = None

    thick = "━" * 18
    parts: list[str] = [
        thick,
        "🟣 <b>PREGAME PLAYER PROP OPPORTUNITY</b>",
        thick,
        "",
        f"<b>Sport:</b>   {s_icon} {entry.sport}",
        f"<b>Player:</b>  {entry.player_name}",
        f"<b>Market:</b>  {entry.stat_type}",
    ]
    if time_str:
        parts.append(f"<b>Game in:</b> {time_str}")
    parts += ["", div]

    _pe = {"PrizePicks": "🟣", "Underdog": "🐶", "DraftKings": "🎰", "FanDuel": "🦊"}

    if comp is not None:
        # ── Available providers only — skip those with no real data ──────────
        avail = [
            (p, comp.lines[p])
            for p in ["PrizePicks", "Underdog", "DraftKings", "FanDuel"]
            if p in comp.lines and comp.lines[p].available and comp.lines[p].line_value is not None
        ]
        n_avail = len(avail)
        header = "<b>📊 Available Line</b>" if n_avail == 1 else "<b>📊 Available Lines</b>"
        parts += ["", header, ""]
        for p, pl in avail:
            parts.append(f"  {_pe[p]} {p}:  <code>{pl.line_value:.1f}</code>")

        # Movement
        if comp.movement is not None and abs(comp.movement) >= 0.01:
            prev = comp.previous_line
            curr = comp.best_line
            sign  = "+" if comp.movement > 0 else ""
            arrow = "↑"  if comp.movement > 0 else "↓"
            ps    = f"{prev:.1f}" if prev is not None else "?"
            cs    = f"{curr:.1f}" if curr is not None else "?"
            parts += [
                "",
                f"<b>📈 Movement:</b>  {ps} → {cs}  <code>{sign}{comp.movement:.1f} {arrow}</code>",
            ]

        # Best available line
        if comp.best_over_app and comp.best_under_app:
            parts += ["", "<b>🏆 Best Available Line</b>", ""]
            oe = _pe.get(comp.best_over_app,  "?")
            ue = _pe.get(comp.best_under_app, "?")
            parts.append(
                f"  ⬆ OVER  → {oe} {comp.best_over_app}  "
                f"<code>{comp.best_over_line:.1f}</code>"
            )
            parts.append(
                f"  ⬇ UNDER → {ue} {comp.best_under_app}  "
                f"<code>{comp.best_under_line:.1f}</code>"
            )
        elif comp.best_line is not None:
            be = _pe.get(comp.best_provider or "", "?")
            parts += [
                "",
                f"<b>🏆 Best Available Line:</b>  {be} {comp.best_provider}  "
                f"<code>{comp.best_line:.1f}</code>",
            ]

        n_prov = sum(1 for pl in comp.lines.values() if pl.available)
        parts += [
            "",
            div,
            "",
            f"<b>Market Quality:</b>  {n_prov}/4 providers",
            f"<b>Confidence:</b>       {comp.proxy_match_confidence}/100",
        ]
        if comp.best_reason:
            parts.append(f"<b>Reason:</b>           {comp.best_reason}")

    else:
        # ── Fallback: opening vs current lines (only show providers with data) ─
        if entry.opening_lines:
            parts += ["", "<b>📊 Opening Lines</b>", ""]
            for p, wl in entry.opening_lines.items():
                parts.append(f"  {_pe.get(p, '?')} {p}:  {wl.line_value:.1f}")

        if entry.current_lines:
            parts += ["", "<b>📈 Current Lines</b>", ""]
            for p, wl in entry.current_lines.items():
                mv     = entry.movement.get(p)
                mv_str = ""
                if mv is not None and abs(mv) >= 0.01:
                    sign  = "+" if mv > 0 else ""
                    arrow = "↑" if mv > 0 else "↓"
                    mv_str = f"  <code>{sign}{mv:.1f} {arrow}</code>"
                parts.append(f"  {_pe.get(p, '?')} {p}:  {wl.line_value:.1f}{mv_str}")

        if entry.has_movement:
            parts += [
                "", div, "",
                "⚠️ <b>Movement Detected</b>",
                f"   {entry.movement_summary()}",
            ]

    parts += ["", div, "", "<i>Verify lines before placing. Markets move fast.</i>", "", thick]
    return "\n".join(parts)


# ── Scanner ───────────────────────────────────────────────────────────────────

class PregameWatchEngine:
    """
    Foundation for the pregame market watch workflow.

    Usage (future scheduler integration):
        engine = PregameWatchEngine()
        await engine.morning_scan(db, now)          # ~9 AM, populates _watch_entries
        await engine.pregame_scan(db, bot, chat_ids, now)  # ~3 h before game, fires alerts
    """

    def __init__(self) -> None:
        # In-memory store: keyed on (player_name, stat_type, game_id)
        self._watch_entries: dict[tuple, PregameWatchEntry] = {}
        # Dedup: first-time alert per (player, stat, game_id) key
        self._alerted_set: set[str] = set()
        # Movement dedup: track the last-alerted line per key so we re-alert only on change
        self._movement_alerted: dict[str, float] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def morning_scan(
        self,
        db:  Any,
        now: Optional[datetime] = None,
    ) -> int:
        """
        Populate watch entries from current Underdog feed for games
        scheduled within UPCOMING_WINDOW_HOURS.

        Returns the number of entries created or updated.
        """
        if now is None:
            now = datetime.utcnow()

        cutoff = now + timedelta(hours=UPCOMING_WINDOW_HOURS)
        n_created = 0

        try:
            # Pull latest Underdog props from PropLineHistory
            ud_props = await db.get_latest_props_for_provider(
                "Underdog", since_hours=2,
            )
        except Exception as exc:
            logger.warning("pregame_watch.morning_scan: DB error: %s", exc)
            return 0

        for row in ud_props:
            game_start = getattr(row, "game_start", None)
            game_id    = getattr(row, "game_id", None)
            if game_start and game_start > cutoff:
                continue   # too far out

            key = (row.player_name, row.stat_type, game_id)
            if key not in self._watch_entries:
                entry = PregameWatchEntry(
                    player_name = row.player_name,
                    sport       = row.sport,
                    stat_type   = row.stat_type,
                    game_id     = game_id,
                    game_start  = game_start,
                    created_at  = now,
                )
                self._watch_entries[key] = entry
                n_created += 1

            # Record opening line from Underdog
            entry = self._watch_entries[key]
            if "Underdog" not in entry.opening_lines:
                entry.opening_lines["Underdog"] = WatchLine(
                    provider   = "Underdog",
                    emoji      = "🐶",
                    line_value = float(row.line_value or 0),
                    fetched_at = now,
                )

        logger.info(
            "pregame_watch.morning_scan: %d entries created/updated "
            "(total=%d) window=%dh",
            n_created, len(self._watch_entries), UPCOMING_WINDOW_HOURS,
        )
        return n_created

    async def pregame_scan(
        self,
        db:       Any,
        bot:      Any,
        chat_ids: list,
        now:      Optional[datetime] = None,
    ) -> int:
        """
        Continuous scan — fires for qualifying props not yet alerted, and
        re-fires when significant line movement is detected.

        Alert conditions:
          • First detection of the prop with proxy_match_confidence ≥ 60
          • Subsequent detection where the current line differs from the
            last-alerted line (movement re-alert)

        Returns the number of alerts sent.
        """
        if now is None:
            now = datetime.utcnow()

        alerts_sent = 0
        pregame_cutoff = now + timedelta(hours=PREGAME_SCAN_HOURS)

        # All entries in the pregame window (none = too far out; game_start None = always included)
        targets = [
            (key, entry)
            for key, entry in self._watch_entries.items()
            if entry.game_start is None or entry.game_start <= pregame_cutoff
        ]
        if not targets:
            logger.debug("pregame_watch.pregame_scan: no entries in window (total=%d)", len(self._watch_entries))
            return 0

        # Bulk fetch cross-provider data once for all targets
        # DK/FD removed from active workflow — Underdog + PrizePicks only
        try:
            from engine.player_prop_market import build_player_prop_market_comparison
            pp_rows = await db.get_latest_props_for_provider("PrizePicks", since_hours=24)
        except Exception as exc:
            logger.warning("pregame_watch.pregame_scan: cross-provider fetch failed: %s", exc)
            pp_rows = []

        # Refresh current Underdog lines
        try:
            ud_current = await db.get_latest_props_for_provider("Underdog", since_hours=1)
            ud_index: dict[tuple, Any] = {
                (r.player_name, r.stat_type): r for r in ud_current
            }
        except Exception as exc:
            logger.warning("pregame_watch.pregame_scan: UD DB error: %s", exc)
            return 0

        from alerts import broadcast_alert

        for key, entry in targets:
            player, stat, _ = key
            alert_key = f"{player}|{stat}|{entry.game_id or ''}"

            # Update current Underdog line
            ud_row = ud_index.get((player, stat))
            if ud_row:
                entry.current_lines["Underdog"] = WatchLine(
                    provider   = "Underdog",
                    emoji      = "🐶",
                    line_value = float(ud_row.line_value or 0),
                    fetched_at = now,
                )

            # Compute movement vs opening lines
            entry.movement = {}
            for p, current in entry.current_lines.items():
                if p in entry.opening_lines:
                    entry.movement[p] = round(
                        current.line_value - entry.opening_lines[p].line_value, 2
                    )
            entry.updated_at = now

            # Resolve best available Underdog line for comparison
            ud_line_val: Optional[float] = None
            if ud_row:
                ud_line_val = float(ud_row.line_value or 0)
            elif "Underdog" in entry.current_lines:
                ud_line_val = entry.current_lines["Underdog"].line_value
            elif "Underdog" in entry.opening_lines:
                ud_line_val = entry.opening_lines["Underdog"].line_value

            if ud_line_val is None:
                continue

            # Build market comparison — Underdog + PrizePicks only
            prev_line = (
                entry.opening_lines["Underdog"].line_value
                if "Underdog" in entry.opening_lines else None
            )

            comp = build_player_prop_market_comparison(
                player_name    = player,
                sport          = entry.sport,
                stat_type      = stat,
                ud_line        = ud_line_val,
                previous_line  = prev_line,
                pp_rows        = pp_rows,
                now            = now,
                min_confidence = 60,   # quality gate for pregame alerts
            )

            if comp is None:
                continue   # below confidence threshold

            # Alert conditions
            is_first_alert   = alert_key not in self._alerted_set
            prev_alerted_val = self._movement_alerted.get(alert_key)
            has_new_movement = (
                entry.has_movement
                and prev_alerted_val != ud_line_val
            )

            if not chat_ids or not (is_first_alert or has_new_movement):
                continue

            try:
                msg    = format_pregame_watch_alert(entry, comp)
                counts = await broadcast_alert(bot, chat_ids, msg)
                if counts.get("sent", 0) > 0:
                    alerts_sent += 1
                    self._alerted_set.add(alert_key)
                    self._movement_alerted[alert_key] = ud_line_val
                    logger.info(
                        "pregame_watch: alert sent — %s / %s  conf=%d  movement=%s",
                        player, stat, comp.proxy_match_confidence, entry.movement_summary(),
                    )
            except Exception as exc:
                logger.warning("pregame_watch: alert failed for %s / %s: %s", player, stat, exc)

        return alerts_sent

    def clear_stale(self, now: Optional[datetime] = None) -> int:
        """Remove entries for games that have already started (plus 30 min buffer)."""
        if now is None:
            now = datetime.utcnow()
        stale = [
            k for k, e in self._watch_entries.items()
            if e.game_start is not None
            and e.game_start < now - timedelta(minutes=30)
        ]
        for k in stale:
            del self._watch_entries[k]
        return len(stale)

    @property
    def watch_count(self) -> int:
        return len(self._watch_entries)


# ── Module-level singleton ────────────────────────────────────────────────────
# Instantiated lazily on first access. The bot can import and call
# get_pregame_watch_engine() from anywhere without circular imports.

_pregame_engine: Optional[PregameWatchEngine] = None


def get_pregame_watch_engine() -> PregameWatchEngine:
    global _pregame_engine
    if _pregame_engine is None:
        _pregame_engine = PregameWatchEngine()
    return _pregame_engine


# ── DK/FD index helper ────────────────────────────────────────────────────────

def _build_dk_fd_index(records: list) -> dict:
    """Parse OddsRecord rows into {(player_lower, sportsbook): line}.

    Selection format stored by _player_props_job:
      "Player Name Over"  →  OVER line
      "Player Name Under" →  UNDER line  (stored only when no OVER entry yet)
    """
    index: dict = {}
    for rec in records:
        sel  = getattr(rec, "selection", "") or ""
        book = getattr(rec, "sportsbook", "") or ""
        line = getattr(rec, "line", None)
        if not sel or line is None:
            continue
        sel_lower = sel.lower()
        if sel_lower.endswith(" over"):
            player_key = sel_lower[:-5].strip()
            index[(player_key, book)] = float(line)
        elif sel_lower.endswith(" under"):
            player_key = sel_lower[:-6].strip()
            if (player_key, book) not in index:   # OVER takes precedence
                index[(player_key, book)] = float(line)
    return index
