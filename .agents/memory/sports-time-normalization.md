---
name: Provider timestamp normalization
description: Rules for UTC instants, date-only values, and user-facing timezones across sports.
---

Provider timestamps with explicit offsets are instants: convert them to UTC before dropping timezone information or extracting a UTC calendar date. Treat naive provider timestamps as UTC where the adapter contract does so, and preserve date-only values without timezone conversion. User-facing display zones remain separate: reports use the configured reporting timezone, while explicitly ET-labeled alerts stay in Eastern Time.

**Why:** Provider feeds mix naive UTC timestamps, explicit offsets, and date-only fields. Stripping an offset or converting a date-only value can shift an event across midnight, causing incorrect event timing, display dates, and game-state classification.

**How to apply:** Normalize only timestamps that carry a real offset; retain naive/date-only semantics. Add tests for offsets crossing midnight and keep same-day reporting boundaries unchanged when correcting provider timestamps.