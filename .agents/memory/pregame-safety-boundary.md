---
name: Pregame delivery boundary
description: Where strict event-state safety applies without corrupting historical reporting
---

Production Telegram delivery and current opportunity displays must require a known pregame event when start time or provider state is incomplete. Historical alert reports must not apply that current-state filter; once an alert was sent, it remains reportable even after the event starts or the source row lacks timing metadata.

**Why:** Applying a current pregame filter to delivered-alert history hid valid past alerts and broke provider/source reporting. The generic timing helper also has legacy callers that intentionally allow missing timestamps, so strictness must be explicit at the production boundary.

**How to apply:** Keep the default timing behavior backward-compatible. Opt into strict known-pregame validation from real delivery paths and `/picks`/`/slip`-style current displays; leave `/alerts`, grading, tracking, and other historical views independent of present event state.