---
name: Underdog grading source
description: Project rule for grading picks sourced from Underdog.
---

Use official Underdog settlement results as the source of truth for Underdog-native grading. Do not infer HIT, MISS, PUSH, or VOID from projected-line movement, disappearance, or unrelated player history. A numeric zero is not proof of a void; only an explicit provider settlement can mark one.

**Why:** The user selected official Underdog settlement results as the grading source; market projections are not settlement outcomes. Zero is also a valid completed stat and must not be removed from hit-rate denominators by a heuristic.

**How to apply:** Integrate only a documented, authorized Underdog result feed or export with event/player/stat identity. An explicit event-scoped void must outrank conflicting numeric history for that event. If no official result is available, state that official grading is blocked and keep any existing third-party/stat-derived grading clearly distinct.