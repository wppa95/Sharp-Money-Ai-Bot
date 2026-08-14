---
name: Tier 2 Telegram delivery backstop
description: Guards at deliver_underdog() call sites are insufficient — direct broadcast_alert() calls bypass them. The final backstop lives inside deliver_underdog() itself.
---

## Rule
Guards at each `deliver_underdog()` call site in market_engine.py are NOT sufficient.
`broadcast_alert()` is called directly from multiple paths that bypass `deliver_underdog()` entirely.

## The full list of bypass paths (Aug 2026)
1. `market_engine.py` — inefficiency alert (`ineff.sport`)
2. `market_engine.py` — steam alert (`sport`)
3. `market_engine.py` — CLV opportunity alert (`snap.sport`)
4. `engine/player_prop_market.py` — player prop market alert (`sport`)

## Correct fix pattern
Add the block at **two levels**:
- **Final backstop inside `deliver_underdog()`** in `alerts.py` — runs unconditionally before any Telegram send, regardless of which call site reached it.
- **Per-call-site guard** at each `broadcast_alert()` call with a sport field.

## Patchable constant
`alerts._TIER2_SPORTS_BLOCK = frozenset({"NBA", "MLB", "NFL"})` at module level.
Tests that need to verify downstream logic (daily cap, timing, scope) must `patch.object(alerts_mod, "_TIER2_SPORTS_BLOCK", frozenset())`.

**Why:** A local `_T2_BLOCK` variable inside the function is not patchable, so sport-specific format tests (emoji, etc.) would have no way to bypass the block.

## Test pattern for sport-icon tests
Sport-icon tests (NFL 🏈, NBA 🏀) that test format function output must patch the block:
```python
with patch.object(alerts_mod, "_TIER2_SPORTS_BLOCK", frozenset()):
    await delivery.deliver_underdog(..., sport="NFL", ...)
assert "🏈" in sent_messages[0]
```
