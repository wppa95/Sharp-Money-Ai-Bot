---
name: Non-MLB funnel populations
description: How to interpret Underdog monitoring, analysis, directional, and delivery populations.
---

The funnel must treat raw observations, normalized history, scored evaluations, directional opportunities, and delivered alerts as separate populations. A full-pool rotation is a real analysis pass even when its safety gates reject every pick, so it must emit evaluation telemetry for neutral and non-MLB results rather than making those sports appear unscanned. Report-window active rows must be a subset of that window's observations. When non-critical candidate telemetry is missing for a short window, directional and confirmed-delivery counts are a conservative floor for evaluated counts so the funnel never reports impossible downstream totals.

**Why:** Reporting only directional rows made large non-MLB feeds look absent even though the provider was ingesting and scoring them; mixing all-time active rows with windowed observations made the funnel show impossible downstream counts.

**How to apply:** Preserve the existing centralized football exclusion and safety gates; improve normalization, evidence, and telemetry in the existing pipeline instead of creating a parallel alert path. Scope latest active rows to the same report window as observations. Keep neutral candidate evaluations when present, then apply a per-sport max with directional and confirmed-delivery populations before presenting the funnel.