---
name: Funnel timing
description: How scan timing should be represented in current-session reports
---

Funnel scan timing must come from observed scan timestamps, not from the requested report-window length.

**Why:** A single scan a few minutes after startup was previously displayed as taking the entire one-hour report window.

**How to apply:** Show a single scan by age and show cadence only as the interval between two or more observed scans.