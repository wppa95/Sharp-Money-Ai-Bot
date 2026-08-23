---
name: Unified prop tracking
description: The monitoring lifecycle treats persistent snapshots as the source of truth, separate from alert delivery.
---

Persistent prop snapshots are monitoring records even when a prop is rejected, non-actionable, evidence-missing, or not delivered. Retired Watchlist/Near-Miss labels must not drive rotation or appear in monitoring reports; legacy rows should be folded into ordinary rejected reporting when surfaced.

**Why:** Delivery gates and scoring are intentionally independent from coverage, grading, and future rescoring. Conflating them caused monitored props to disappear from downstream history.

**How to apply:** Build recent dashboard views from persistent PropLineHistory data across all sports, and keep recommendation/tier values only where existing scoring or delivery compatibility requires them.