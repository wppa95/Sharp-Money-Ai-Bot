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

**Why:** The user's pick-to-delivery follow-up explicitly separates movement
notifications from picks, superseding older market-move suppression.

**How to apply:** Keep source/event/transition-aware notification dedup separate
from betting-pick dedup, immutable delivery evidence, grading, and performance.
Do not re-enable retired sportsbook providers or multi-pick delivery.
