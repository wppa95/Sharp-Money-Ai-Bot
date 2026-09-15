---
name: Telegram alert presentation
description: Durable rules for keeping prop alerts compact, bettor-facing, and free of internal scoring vocabulary.
---

Prop alert formatting is a presentation boundary: preserve the underlying score, confidence, qualification, and evidence values, but render only plain bettor-facing labels and one dedicated history section.

**Why:** Repeated windows in both the history block and generated reason made alerts harder to scan, while enum/class and internal quality labels exposed implementation details.

**How to apply:** Use a plain `Quality: N/100` line with the existing star mapping, normalize market labels, convert short season samples to sample labels, and remove history/internal tier clauses from reasons without changing scoring or delivery logic.

Player-facing player-prop picks should omit AI/value-analysis narratives, show the line as a simple value rather than “Player Prop Line,” and never describe a line as being at a historical low.

**Why:** The user requested simple, clean Telegram picks focused on the sport, player, line, pick, and history; comparison prose and internal market labels obscured the actionable content.

**How to apply:** Keep the underlying comparison and historical-low fields for internal scoring/evidence, but do not render their analyst explanation or historical-low wording in sent pick messages.

Underdog and PrizePicks/PropLine are one user-facing Player Props product, even
though their ingestion, provider scope, persistence, and reconciliation remain
independent internally.

**Why:** Users want one consistent player-prop experience, while provider
identity is still required for source correctness, alternate-line tracking,
health diagnostics, and grading.

**How to apply:** Render alerts and command displays as `Player Prop` or
`Player Props`; keep provider names only as optional source details or internal
database/log identity.