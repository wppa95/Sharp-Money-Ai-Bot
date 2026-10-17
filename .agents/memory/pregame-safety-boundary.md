---
name: Pregame delivery boundary
description: Where strict event-state safety applies without corrupting historical reporting
---

Production Telegram picks require a verified future event on the current
America/Chicago calendar day. Send qualified same-day individual picks
immediately, even more than two hours before the game. Block next-day games
until the Central calendar date changes. Missing start timestamps cannot prove
the reporting day and must remain delivery-blocked, even with a pregame status.
Historical alert reports remain independent of the current event state.

**Why:** The user's final policy restored same-day eligibility while keeping
immediate qualified delivery. Missouri's America/Chicago zone, including DST,
defines the day and display time; it is not a fixed UTC offset.

**How to apply:** Apply the same Central-day rule to both primary platforms.
Preserve UTC event instants and all pregame/source/freshness checks. Two-hour
game reminders are separate operational notifications, never a reason to delay
the initial pick. A pick qualifying inside two hours sends immediately without
a late two-hour reminder. Keep historical reports independent of present state.