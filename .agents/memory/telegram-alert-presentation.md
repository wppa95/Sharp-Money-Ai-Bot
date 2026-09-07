---
name: Telegram alert presentation
description: Durable rules for keeping prop alerts compact, bettor-facing, and free of internal scoring vocabulary.
---

Prop alert formatting is a presentation boundary: preserve the underlying score, confidence, qualification, and evidence values, but render only plain bettor-facing labels and one dedicated history section.

**Why:** Repeated windows in both the history block and generated reason made alerts harder to scan, while enum/class and internal quality labels exposed implementation details.

**How to apply:** Use a plain `Quality: N/100` line with the existing star mapping, normalize market labels, convert short season samples to sample labels, and remove history/internal tier clauses from reasons without changing scoring or delivery logic.

Underdog and PrizePicks/PropLine are one user-facing Player Props product, even
though their ingestion, provider scope, persistence, and reconciliation remain
independent internally.

**Why:** Users want one consistent player-prop experience, while provider
identity is still required for source correctness, alternate-line tracking,
health diagnostics, and grading.

**How to apply:** Render alerts and command displays as `Player Prop` or
`Player Props`; keep provider names only as optional source details or internal
database/log identity.