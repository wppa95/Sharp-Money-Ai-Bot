---
name: Direction-only prop delivery
description: Active prop delivery and strongest-pick selection are independent of tier, bet quality, market quality, and PASS labels.
---

The active delivery policy is recommendation plus complete directional history. Telegram keeps every eligible OVER pick with L5/L10/L15/L20/L30/Season all at least 70% OVER, or UNDER pick with all six windows at most 40% OVER; missing windows and historical-average-only fallbacks never deliver. There is no global or per-sport count cap.

**Why:** Score-class gates caused qualified evidence-backed props to be rejected or inconsistently handled between scan paths and final Telegram delivery. Historical-average provenance can live directly on a decision object, so both the collection gate and the Telegram formatter must inspect that field instead of requiring serialized validation JSON.

**How to apply:** New delivery paths should call the shared directional/evidence gate after strict six-window history validation; retain incomplete or mixed histories for tracking/grading only, and represent no actionable direction as `None`.