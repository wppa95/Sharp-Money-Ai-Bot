---
name: Optional score reporting
description: Defensive handling for incomplete score fields in monitoring diagnostics.
---

Monitoring diagnostics must treat a missing score as a reportable `N/A` value, not as a sortable or format-required integer.

**Why:** Scoring can produce partial candidate metadata during provider transitions or test/custom-provider paths. A debug summary must never turn that incomplete metadata into a failed live monitoring cycle.

**How to apply:** Use a deterministic fallback sort key for missing scores and a safe display formatter whenever diagnostic ranking output reads optional score fields.