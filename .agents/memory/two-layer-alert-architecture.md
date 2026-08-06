---
name: Two-layer alert architecture
description: Detection layer vs. betting pick layer — two distinct Telegram alert formats, removal suppression, availability status, per-sport pipeline diagnostics.
---

## Rule
Alert pipeline now has two separate Telegram categories:

**A) `📈 MARKET MOVE DETECTED`** — `format_market_move_detected()` in `alerts_multiplatform.py`
- Fires when a line moves ≥ MIN_UNDERDOG_LINE_CHANGE but does NOT pass the full scoring/confidence/tier gate
- Lightweight: shows old→new line, movement amount, "Status: Tracking — evaluating for bet quality"
- Routed via `deliver_underdog(..., market_move_only=True)` — new keyword-only param

**B) `🎯 ACTIONABLE BET PICK`** — `format_underdog_change_alert()` header updated
- Fires only when prop is fully qualified (stars gate + OVER/UNDER decision + tier confidence gate)
- Shows availability status: 🟢 AVAILABLE NOW / 🟡 CLOSING SOON (≤30 min to game) / 🔴 GAME LIVE
- Also used for standing opportunity alerts (was "UNDERDOG STANDING PLAY")

## Removal alerts
- Removal Telegram alerts **suppressed** entirely (doc #2/#3)
- `is_qualified = False` for all removals; `should_alert` no longer includes `is_removed`
- Lifecycle tracking (REMOVED state) still applied via `_lifecycle_removed` decoupled from `ud_result.sent`: queued when `is_removed and prev_record is not None`
- Market availability window (first alert → removal) logged internally via `_MARKET_FIRST_ALERT` module-level dict

## Per-sport pipeline diagnostics
Three stage counters added to `underdog_job`:
- `_sport_raw`: non-removed snaps from Underdog, per sport
- `_sport_parsed`: after futures filter passes, per sport
- `_sport_gated`: passed full betting gate (would send alert), per sport

Emitted in `underdog_job [debug summary]` INFO line as a per-sport table.

## DK/FD disabled by default
`DRAFTKINGS_ENABLED` and `FANDUEL_ENABLED` defaults changed to `"false"` (config.py).
Re-enable with env vars when adding back provider-by-provider.

**Why:** Underdog-only validation phase; doc explicitly prioritizes Underdog pipeline before adding other providers.

## Key invariant
`_market_move_sent` is initialized at the per-prop variable block (alongside `ud_result`) so all code paths are safe regardless of which scoring branch runs.
