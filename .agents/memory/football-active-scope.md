---
name: Football active-scope policy
description: NFL and college football are allowed; other football-family sports remain excluded from actionable delivery.
---

User-facing actionable delivery allows NFL and CFB/NCAAF aliases, and rejects
other football-related provider sports such as FCS, CFL, AFL/AFLW, UFL,
gridiron, rugby, and unrelated provider identifiers containing football.
Association soccer is not part of this block. Provider observations may still
be retained for persistence and historical diagnostics.

**Why:** The product wants NFL and college-football picks but not other
football-family markets. Filtering only the disallowed aliases prevents
unsupported competitions from reaching user-facing alerts without deleting
evidence or changing historical reports.

**How to apply:** Reuse the disallowed-football predicate in every actionable
Telegram and provider-history path, including direct pregame-watch broadcasts.
Keep provider observations and historical rows intact, and keep
AlertScopeFilter available for unrelated scope violations.