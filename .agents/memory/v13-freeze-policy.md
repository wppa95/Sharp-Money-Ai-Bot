---
name: V1.3 freeze policy
description: V1.3 is frozen; defines what work is and is not allowed going forward.
---

## Policy

V1.3 was frozen in August 2026 after stable-refresh + watchlist rescanning (Task #121) was merged.

**Allowed**: bug fixes, stability improvements, performance optimisations, data-correctness fixes, provider/connectivity fixes.

**Not allowed**: new features, architectural changes.

**Why**: The codebase has grown significantly. Freezing V1.3 prevents scope creep and lets the alert pipeline stabilise before any new capabilities are added.

**How to apply**: Any proposed task should be evaluated against this policy before starting. If it adds new user-visible functionality or restructures core abstractions, it does not qualify under V1.3.
