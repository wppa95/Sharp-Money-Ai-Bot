---
name: FPR cursor fairness
description: Fair full-pool rotation ordering and prompt-work limits.
---

The full-pool rescan must keep one stable, identity-ordered active universe. The persisted cursor window is the guaranteed primary population; new or moving props may use only bounded prompt capacity outside that window.

**Why:** Sorting the whole pool by freshness or quality lets continuously new or moving rows consume every batch and leaves unchanged active props unrescanned.

**How to apply:** Never use recommendation, tier, confidence, evidence, rejection, delivery, or sport-priority metadata in the canonical order. Advance the cursor across the complete cursor window regardless of prompt work, and keep one-item batches cursor-only.