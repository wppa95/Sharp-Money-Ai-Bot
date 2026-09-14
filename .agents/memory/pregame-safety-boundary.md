---
name: Pregame delivery boundary
description: Where strict event-state safety applies without corrupting historical reporting
---

Production Telegram delivery and current opportunity displays normally require a known pregame event when start time or provider state is incomplete. A narrow configured exception permits CS, TENNIS, and FIFA active-feed picks through when only the timestamp is missing; explicit live/final states and known past times remain blocked. Historical alert reports must not apply this current-state filter; once an alert was sent, it remains reportable even after the event starts or the source row lacks timing metadata.

**Why:** Applying a current pregame filter to delivered-alert history hid valid past alerts and broke provider/source reporting. Several active Underdog feeds omit scheduled_at, so enforcing known timing there suppressed otherwise evidence-backed picks; the exception stays opt-in by sport and does not bypass explicit live/final status checks.

**How to apply:** Keep strict known-pregame validation for sports with reliable event metadata and use the configured unknown-pregame sport set only for missing timestamps. Leave `/alerts`, grading, tracking, and other historical views independent of present event state.