---
name: Telegram alert presentation
description: Durable rules for keeping prop alerts compact, bettor-facing, and free of internal scoring vocabulary.
---

Prop alert formatting is a presentation boundary: preserve the underlying score, confidence, qualification, and evidence values, but render only plain bettor-facing labels and one dedicated history section.

**Why:** Repeated windows in both the history block and generated reason made alerts harder to scan, while enum/class and internal quality labels exposed implementation details.

**How to apply:** Use a plain `Quality: N/100` line with the existing star mapping, normalize market labels, convert short season samples to sample labels, and remove history/internal tier clauses from reasons without changing scoring or delivery logic.