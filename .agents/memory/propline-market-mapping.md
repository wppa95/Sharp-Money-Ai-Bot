---
name: PropLine cross-sport market mapping
description: Sport-specific disambiguation rules for PropLine player markets.
---

PropLine market resolution must apply sport-specific rules before generic stat aliases. Soccer shots and goal-plus-assist are not hockey shots-on-goal or generic points, and CS2 three-map labels must never route to the documented two-map market.

**Why:** The same display label is reused across sports, so generic normalization can silently send a valid request to the wrong official market or treat unsupported CS2 coverage as supported.

**How to apply:** Preserve provider-native market identity when it is available; otherwise resolve with the canonical sport first and return no market for unsupported combinations instead of guessing.