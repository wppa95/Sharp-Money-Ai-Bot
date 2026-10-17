---
name: Two-layer alert architecture
description: Factual LINE CHANGE notifications remain separate from independently qualified individual betting picks.
---

Verified source-owned movements may send factual `LINE CHANGE` notifications.
They are not betting picks, must not replace the original pick's line or
direction, and must not establish a delivered PLAY or grading outcome.
Display the platform, observed old/new lines, player, market, and direction
only when known. Keep removals internal.

New individual betting picks require independently verified current selectable
lines and the existing qualification/safety checks. A movement alone cannot
create a pick. Underdog and PrizePicks have equal status.

Two-hour game reminders are a third message purpose within operational notices.
They refer to an original verified selection and event; they never create a
second pick or modify its line, direction, qualification, or delivery record.
Reminder restarts/repeated evaluation must not duplicate notifications, and
invalidated selections/events must cancel or update pending reminders.

**Why:** The user explicitly separates immediate picks, factual movements, and
two-hour game reminders. Synchronizing these messages would delay picks or
misrepresent notifications as new bets.

**How to apply:** Keep source/event/transition-aware notification dedup separate
from betting-pick dedup, immutable delivery evidence, grading, and performance.
Do not re-enable retired sportsbook providers or multi-pick delivery.
