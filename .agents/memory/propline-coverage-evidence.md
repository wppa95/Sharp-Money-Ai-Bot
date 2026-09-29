---
name: PropLine completed-evidence coverage
description: Rules for rotating and classifying non-MLB PropLine history attempts
---

For the event-results and resolved-history routes, only rows with an explicit settled outcome (WON, LOST, PUSH, or VOID) are settlement evidence. Separately, a numeric player-game row may grade a directional pick only after final/completed status and exact event/player/sport/stat matching; it never establishes a void.

**Why:** A provider can return a successful-looking payload whose entries are unresolved or entitlement-redacted. Final player-game stats have a narrower purpose: they resolve a saved numeric line only when the completed event and stat identity are exact.

**How to apply:** Keep the one-target-per-cycle PropLine rotation cursor, preserve timed-out targets for retry, and record attempt outcomes by canonical sport and official market so every supported non-MLB target can be audited across a full rotation.

The read-only `player_history` response is not the same type as event settlement evidence and has no `is_settled` property. Its settlement-history converter still requires explicit WON/LOST/PUSH/VOID resolution. The separate final player-game parser requires an explicit final/completed status, event ID, date, and numeric actual value.

**Why:** Passing player-history entries to the event-result converter silently drops them, while accepting unresolved entries would turn unsettled provider observations into settlement evidence.

**How to apply:** Reuse the strict player-history entry converter for `player_history` rotation results; keep completeness/redaction checks and request budgets. Never substitute final player-game stats for explicit event settlement rows.