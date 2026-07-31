"""
pregame_watch.py — Pregame Market Watch Foundation.

Monitors upcoming games before alerts fire, tracking opening lines and
identifying meaningful movement across providers in the hours before tip-off.

This is a FOUNDATION module — data structures and scan logic only.
The scheduler integration is intentionally omitted until live testing
confirms the pattern. No active job is registered here.

Workflow (when activated):
  Morning scan (e.g. 9 AM):
    - Identify upcoming games for the day
    - Record available player prop opening lines per provider
    - Store in PregameWatchEntry for comparison

  Pre-game scan (~3 h before tip-off):
    - Re-fetch all lines for watched games
    - Compute movement vs opening lines
    - Flag meaningful moves (threshold: ≥0.5 line units or provider divergence)
    - Emit a PREGAME WATCH alert if criteria met

  Alert format:  📋 PREGAME MARKET WATCH
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

def format_pregame_watch_alert(entry: PregameWatchEntry) -> str:
    """
    Format a 📋 PREGAME MARKET WATCH alert for Telegram (HTML).

    Only called when entry.has_movement is True or provider divergence exists.
    """
    sport_icons = {
        "NFL": "🏈", "NBA": "🏀", "MLB": "⚾", "NHL": "🏒",
        "UFC": "🥊", "WNBA": "🏀", "SOCCER": "⚽", "TENNIS": "🎾",
    }
    s_icon = sport_icons.get(entry.sport.upper(), "🎯")

    div = "─" * 16

    mins = entry.minutes_to_game()
    time_str = (
        f"{int(mins)} min" if mins is not None and mins >= 0
        else (entry.game_start.strftime("%H:%M UTC") if entry.game_start else "—")
    )

    parts: list[str] = [
        "📋 <b>PREGAME MARKET WATCH</b>",
        "",
        f"{s_icon} <b>{entry.sport}</b>  ·  {entry.stat_type}",
        f"👤 <b>{entry.player_name}</b>",
        f"⏰ Game in:  {time_str}",
        "",
        div,
        "",
        "📊 <b>Opening Lines</b>",
        "",
    ]

    _provider_emojis = {
        "PrizePicks": "🟣", "Underdog": "🐶",
        "DraftKings": "🎰", "FanDuel": "🦊",
    }
    for p in ["PrizePicks", "Underdog", "DraftKings", "FanDuel"]:
        if p in entry.opening_lines:
            wl = entry.opening_lines[p]
            parts.append(f"{_provider_emojis.get(p, '?')} {p}:  {wl.line_value:.1f}")
        else:
            parts.append(f"{_provider_emojis.get(p, '?')} {p}:  Unavailable")

    if entry.current_lines:
        parts += ["", "<b>📈 Current Lines</b>", ""]
        for p in ["PrizePicks", "Underdog", "DraftKings", "FanDuel"]:
            if p in entry.current_lines:
                wl  = entry.current_lines[p]
                mv  = entry.movement.get(p)
                mv_str = ""
                if mv is not None and abs(mv) >= 0.01:
                    sign  = "+" if mv > 0 else ""
                    arrow = "↑" if mv > 0 else "↓"
                    mv_str = f"  <code>{sign}{mv:.1f} {arrow}</code>"
                parts.append(f"{_provider_emojis.get(p, '?')} {p}:  {wl.line_value:.1f}{mv_str}")
            elif p in entry.opening_lines:
                parts.append(f"{_provider_emojis.get(p, '?')} {p}:  Unavailable now")

    if entry.has_movement:
        parts += [
            "",
            div,
            "",
            f"⚠️ <b>Movement Detected</b>",
            f"   {entry.movement_summary()}",
        ]

    parts += [
        "",
        div,
        "",
        f"<i>Pregame watch — verify lines before placing.</i>",
    ]
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
        For each watched game starting within PREGAME_SCAN_HOURS:
          1. Re-fetch current lines.
          2. Compute movement vs opening lines.
          3. Broadcast a PREGAME MARKET WATCH alert if movement threshold met.

        Returns the number of alerts sent.
        """
        if now is None:
            now = datetime.utcnow()

        alerts_sent = 0
        pregame_cutoff = now + timedelta(hours=PREGAME_SCAN_HOURS)

        # Identify entries with games starting soon
        targets = [
            (key, entry)
            for key, entry in self._watch_entries.items()
            if (
                entry.game_start is None
                or entry.game_start <= pregame_cutoff
            )
        ]
        if not targets:
            logger.debug("pregame_watch.pregame_scan: no upcoming games in window")
            return 0

        # Refresh current Underdog lines
        try:
            ud_current = await db.get_latest_props_for_provider("Underdog", since_hours=1)
            ud_index: dict[tuple, Any] = {
                (r.player_name, r.stat_type): r for r in ud_current
            }
        except Exception as exc:
            logger.warning("pregame_watch.pregame_scan: DB error: %s", exc)
            return 0

        from alerts import broadcast_alert

        for key, entry in targets:
            player, stat, _ = key

            # Update current Underdog line
            ud_row = ud_index.get((player, stat))
            if ud_row:
                entry.current_lines["Underdog"] = WatchLine(
                    provider   = "Underdog",
                    emoji      = "🐶",
                    line_value = float(ud_row.line_value or 0),
                    fetched_at = now,
                )

            # Compute movement
            entry.movement = {}
            for p, current in entry.current_lines.items():
                if p in entry.opening_lines:
                    entry.movement[p] = round(
                        current.line_value - entry.opening_lines[p].line_value, 2
                    )

            entry.updated_at = now

            # Alert if movement exceeds threshold
            if not entry.has_movement:
                continue
            if not chat_ids:
                continue

            try:
                msg    = format_pregame_watch_alert(entry)
                counts = await broadcast_alert(bot, chat_ids, msg)
                if counts.get("sent", 0) > 0:
                    alerts_sent += 1
                    logger.info(
                        "pregame_watch: alert sent — %s / %s  movement=%s",
                        player, stat, entry.movement_summary(),
                    )
            except Exception as exc:
                logger.warning(
                    "pregame_watch: alert failed for %s / %s: %s",
                    player, stat, exc,
                )

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
