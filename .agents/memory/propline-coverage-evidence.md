---
name: PropLine completed-evidence coverage
description: Rules for rotating and classifying non-MLB PropLine history attempts
---

Only rows with an explicit settled outcome (WON, LOST, PUSH, or VOID) are usable completed-game evidence. A complete response with no settled rows is unavailable, while redacted and malformed responses remain distinct.

**Why:** A provider can return a successful-looking payload whose entries are unresolved or entitlement-redacted; treating that payload as evidence would let directional decisions rely on a game that has not actually settled.

**How to apply:** Keep the one-target-per-cycle PropLine rotation cursor, preserve timed-out targets for retry, and record attempt outcomes by canonical sport and official market so every supported non-MLB target can be audited across a full rotation.