---
name: V3.2 post-freeze cleanup
description: Three presentation/persistence fixes — /alerts wording, 12-hour AM/PM time display, and dedup dict restored from DB after restart.
---

## Issue 1 — /alerts wording

alert_sent=True is set only when broadcast_alert() returns sent=True (Telegram API
call succeeded, no TelegramError). No telegram_message_id is stored — only a
boolean + UTC alert_sent_at timestamp. "Delivered" is unprovable; changed to "sent".

Changes in commands.py:
- "all-time delivered" → "all-time sent" (both display and empty-case fallback)
- "N shown" → "N shown (last 72h)" to clarify the display window
- Added comment: "sent = Telegram API accepted the send; no message ID stored"

The underlying query (PropOpportunityLog.alert_sent=True, since_hours=72) is unchanged.

## Issue 2 — User-facing time format

Added _fmt_user_ts(dt: Optional[datetime]) -> str to commands.py.
Output: "Aug 08 · 5:05 PM" — 12-hour clock, no leading zero, month-day prefix.
Returns "—" for None. Uses .strftime("%I").lstrip("0") or "12" for midnight edge case.

Applied to two locations previously using "%H:%M UTC":
- cmd_ev: r.detected_at (EV alert record timestamp)
- cmd_alerts: r.alert_sent_at (per-alert row timestamp)

Stored timestamps (DB, health.json, scheduler) are untouched — UTC throughout.

## Issue 3 — Restart persistence (dedup dict)

The main in-memory state lost on restart was _prop_market_alerted — the dedup dict
that suppresses re-alerting the same prop within UD_ALERT_DEDUP_WINDOW (3600s).
After a restart this was empty, risking duplicate alerts if a prop still qualified.

Fix (minimum change, no new tables):
1. database.py: added get_recent_alerted_props_for_dedup(since_hours=24) — queries
   PropOpportunityLog.alert_sent=True and returns {(player,sport,stat): (ts_unix, line)}
2. market_engine.py: _init_state_from_db() now declares "global _prop_market_alerted"
   and calls get_recent_alerted_props_for_dedup with window = min(24, max(2, 2×dedup_h)).
   Pre-existing in-memory entries are never overwritten. Failure is non-fatal (DEBUG log).

All other core state already survived restarts via DB:
PropLineHistory, PropOpportunityLog, CLVRecord, AlertCLVSeed, _MARKET_FIRST_ALERT (24h).

## Test count
3563 passing Aug 2026. Focused tests in tests/test_v32_post_freeze_cleanup.py (54 tests).
