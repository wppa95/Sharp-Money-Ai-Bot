---
name: Non-MLB funnel populations
description: How to interpret Underdog monitoring, analysis, directional, and delivery populations.
---

The funnel must treat raw observations, normalized history, scored evaluations, directional opportunities, and delivered alerts as separate populations. A full-pool rotation is a real analysis pass even when its safety gates reject every pick, so it must emit evaluation telemetry for neutral and non-MLB results rather than making those sports appear unscanned.

**Why:** Reporting only directional rows made large non-MLB feeds look absent even though the provider was ingesting and scoring them.

**How to apply:** Preserve the existing centralized football exclusion and safety gates; improve normalization, evidence, and telemetry in the existing pipeline instead of creating a parallel alert path.