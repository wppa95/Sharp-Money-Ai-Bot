---
name: Slip journal + player memory
description: SlipJournal/SlipJournalLeg ORM tables, CRUD methods, /slip subcommands, /player, /slipstats
---

## Tables added to database.py (before `# ── Database manager`)
- `SlipJournal` — one row per user-recorded slip (slip_code, stake, slip_type, status OPEN→GRADED/VOID, payout, roi_pct, graded_at)
- `SlipJournalLeg` — one row per leg (slip_code FK, opp_id FK→PropOpportunityLog.id, player_name, stat_type, direction, tier, confidence, result PENDING→HIT/MISS/PUSH)

Both tables created by `Base.metadata.create_all()` at startup — no manual migration needed.

## Database CRUD methods added
- `create_slip_journal(stake, notes) → str` — auto-increments slip_code as SLP-001, SLP-002…
- `get_open_slip_journal() → Optional[SlipJournal]` — most recent OPEN slip
- `add_slip_journal_leg(slip_code, player_name, stat_type, *, opp_id, …) → SlipJournalLeg`
- `get_slip_journal_legs(slip_code) → list`
- `grade_slip_journal(slip_code, payout) → dict` — auto-grades legs with opp_id from PropOpportunityLog; legs without opp_id stay PENDING
- `get_slip_journal_history(limit) → list`
- `get_slip_journal_stats() → dict` — aggregates by slip_type (2-man/3-man/etc), total staked/payout/ROI
- `find_opportunity_for_slip(query) → Optional[PropOpportunityLog]` — numeric=lookup by id, text=fuzzy player name match
- `get_player_prop_history(player_name, limit) → list` — ILIKE partial match
- `get_pick_accuracy_by_sport(limit_sports) → list[dict]` — aggregates HIT/MISS/PUSH per sport from prop_opportunity_log

## Commands
- `/slip create [stake]` — creates OPEN journal entry, returns slip_code
- `/slip add <name or ID>` — adds leg to open slip via `find_opportunity_for_slip`
- `/slip grade [payout]` — auto-grades legs, shows HIT/MISS summary
- `/slip journal` (also `j`, `history`) — shows recent journal history
- `/slip [N]` — existing behavior (optimized correlation slip, unchanged)
- `/player <name>` — shows hit rate, recent picks, warns if hit rate < 30% (cmd_player)
- `/slipstats` — pick accuracy by sport + slip journal win/loss/ROI by size (cmd_slipstats)

## Registration
`cmd_player` and `cmd_slipstats` imported in main.py and registered as `/player` and `/slipstats`.
`_cmd_slip_journal` is a module-level helper in commands.py; cmd_slip dispatches to it when args[0] in JOURNAL_SUBCMDS.

**Why:** Slip journaling keeps track of actual bets placed vs bot recommendations. `opp_id` links to PropOpportunityLog so grade_slip_journal auto-populates from existing grading infrastructure — no duplicate grading logic needed.

**How to apply:** When adding a new slip subcommand, add its keyword to `_JOURNAL_SUBCMDS` in cmd_slip and handle it in `_cmd_slip_journal`.
