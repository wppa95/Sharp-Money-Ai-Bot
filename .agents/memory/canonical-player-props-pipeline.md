---
name: Canonical Player Props pipeline
description: Shared internal normalization and persistence boundary for PropLine and Underdog observations.
---

Both PropLine and Underdog observations enter the same canonical Player Props boundary before evaluation. Provider names, source IDs, event IDs, and line variants remain attached so alternate lines, pacing diagnostics, grading, and reconciliation stay source-correct.

**Why:** User-facing unification alone left ingestion and persistence behavior split, making it easy for one feed to bypass shared history or lose source identity.

**How to apply:** Keep provider-specific fetch/auth/pacing outside the boundary, but normalize, source-deduplicate, and persist both feeds through the canonical pipeline before scoring or delivery. Use the shared PropLineHistory stream for current-state reporting while retaining raw provider ledgers for complete-feed evidence.