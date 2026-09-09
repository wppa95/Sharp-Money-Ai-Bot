---
name: Opportunity event identity
description: Rules for preserving Underdog event IDs through opportunity persistence and reconciliation.
---

Underdog opportunity writes must derive event identity from the snapshot's supported event/game fields, and a later blank observation must never erase a stored nonblank identity.

**Why:** Older rows showed blank game IDs even though retained Underdog snapshots had the event identity. This weakened event-specific settlement reconciliation and made reports imply that evidence was missing.

**How to apply:** Keep event identity separate from team/provider labels. Use exact event/date reconciliation for settlement; historical player/stat rows can explain evidence coverage but must never be used as a guessed settlement.

Large append-only snapshot repairs must remain explicit maintenance operations rather than startup migrations.

**Why:** A correlated repair over the live snapshot ledger delayed bot startup and blocked health readiness.

**How to apply:** Fix identity on the live write path, and run historical repair only through a controlled maintenance invocation.