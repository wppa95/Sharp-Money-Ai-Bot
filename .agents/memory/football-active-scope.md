---
name: Football active-scope policy
description: NFL and CFB are allowed; other football-family sports remain excluded from actionable delivery.
---

User-facing actionable delivery allows NFL and active CFB. Accept NCAAF only
when explicitly identified as the CFB provider alias; reject other
football-related sports such as FCS, CFL, AFL/AFLW, UFL, rugby, and unrelated
provider identifiers containing football. Association soccer is separate.
Provider observations and shared player history remain available for supported
sports.

**Why:** The product wants NFL and CFB picks but not other
football-family markets. Filtering only the disallowed aliases prevents
unsupported competitions from reaching user-facing alerts without deleting
evidence or changing historical reports.

**How to apply:** Apply the disallowed-football predicate only at actionable
delivery boundaries, including direct pregame-watch broadcasts. Never use it
to suppress supported-sport provider observations or shared player history.
Keep AlertScopeFilter available for unrelated scope violations.