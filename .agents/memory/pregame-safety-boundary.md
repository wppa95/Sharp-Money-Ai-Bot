---
name: Pregame delivery boundary
description: Where strict event-state safety applies without corrupting historical reporting
---

Production Telegram delivery and current opportunity displays require a known pregame event when start time or provider state is incomplete. Missing timestamps are not an actionable-delivery exception for any sport; only an explicit pregame provider state can make a missing timestamp safe. Historical alert reports must not apply this current-state filter; once an alert was sent, it remains reportable even after the event starts or the source row lacks timing metadata.

**Why:** Applying a current pregame filter to delivered-alert history hid valid past alerts and broke provider/source reporting. Several active Underdog feeds omit scheduled_at; allowing those rows through with blank or stale status produced post-start/post-game picks, so unknown timing must fail closed at the Telegram boundary.

**How to apply:** Keep strict known-pregame validation on every actionable Telegram path. Treat monitoring/evidence persistence separately from delivery, and leave `/alerts`, grading, tracking, and other historical views independent of present event state.