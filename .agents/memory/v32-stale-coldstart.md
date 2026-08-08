---
name: V3.2 stale diagnostic + cold_start cleanup
description: Two targeted fixes — health.json timestamp parsing bug causing stale Aug 06 errors to always show, and _HFS expansion allowing half-game/per-game Tier 1 stats through the standing path.
---

## Fix 1 — Health timestamp parsing bug

`_now_iso()` returns `"YYYY-MM-DD HH:MM:SS UTC"`. The `/health` command tried `datetime.fromisoformat(...replace("Z", "+00:00"))` which CANNOT handle the " UTC" suffix in Python 3.11 → exception → `_show_global_err = True` → stale errors always shown.

**Fix:** Strip " UTC" before parsing: `.replace(" UTC", "").replace("Z", "").strip()` then `.replace(tzinfo=timezone.utc)`.
Applied to both global last_error AND pipeline failure timestamp in `cmd_health`.

**Why:** Python 3.11 fromisoformat does not accept timezone names; it needs +00:00 offset or no suffix.

## Fix 2 — _HIGH_FLOOR_STATS expansion (cold_start / standing path)

cold_start labels in /funnel are a display artifact: PropCandidateLog stores cold_start entries for all scored props; the standing path logs to PropOpportunityLog. The actual blocker for stable non-HFS Tier 1 props (1H PRA, CoD kills per game) was the `_st not in _HFS` gate in the standing path.

**Added to _HIGH_FLOOR_STATS in `engine/ud_scoring.py`:**
- `"1H Points"`, `"1H Rebounds"`, `"1H Assists"`, `"1H Pts + Rebs + Asts"` (basketball half-game)
- `"Kills on Game 1"`, `"Kills on Game 2"`, `"Assists on Game 1"`, `"Assists on Game 2"` (CoD/esports per-game)

**Why:** These are aggregate multi-component lines with comparable reliability to their full-game equivalents already in _HFS. After cold_start, stable props with no line movement only reach Telegram via the standing path; the _HFS gate was silently blocking all these variants.

## /restarts command
Already removed in a prior pass — confirmed absent.

## Test count
3418 passing Aug 2026.
