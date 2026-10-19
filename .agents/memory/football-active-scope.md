---
name: Football active-scope policy
description: Football-family data remains monitored and stored, but no football-family picks are delivered through Telegram.
---

All football-family sports and aliases are database-monitoring-only. This
includes NFL, CFB/NCAAF, other catalog football competitions, rugby, and
Australian-rules football. Association soccer is separate. Keep collection,
observation persistence, analysis, history, and grading active wherever their
existing validated data paths support them.

**Why:** The user changed the product policy to prohibit Telegram delivery for
every football-related sport while retaining database monitoring and evidence.

**How to apply:** Use the broad football-family predicate at the final Telegram
send boundary and at alert/retry prefilters. Never use it to disable ingestion,
erase records, or suppress verified history/grading. Do not block soccer or
other non-football sports.