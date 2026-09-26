---
name: PropLine completed-evidence coverage
description: Rules for rotating and classifying non-MLB PropLine history attempts
---

Only rows with an explicit settled outcome (WON, LOST, PUSH, or VOID) are usable completed-game evidence. A complete response with no settled rows is unavailable, while redacted and malformed responses remain distinct.

**Why:** A provider can return a successful-looking payload whose entries are unresolved or entitlement-redacted; treating that payload as evidence would let directional decisions rely on a game that has not actually settled.

**How to apply:** Keep the one-target-per-cycle PropLine rotation cursor, preserve timed-out targets for retry, and record attempt outcomes by canonical sport and official market so every supported non-MLB target can be audited across a full rotation.

The read-only `player_history` response is not the same type as event settlement evidence and has no `is_settled` property. Convert its entries only through the player-history parser: require event ID, date, numeric actual value, and explicit WON/LOST/PUSH/VOID resolution before writing numeric game rows.

**Why:** Passing player-history entries to the event-result converter silently drops them, while accepting unresolved entries would turn unsettled provider observations into completed-game evidence.

**How to apply:** Reuse the strict player-history entry converter for already-fetched `player_history` rotation results; keep response completeness/redaction checks and the rotation’s existing request budget.