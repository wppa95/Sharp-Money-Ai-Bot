---
name: Command reporting boundaries
description: Rules for keeping Telegram reports honest and population-specific.
---

User-facing reports must distinguish observations, tracked rows, evaluated candidates, directional opportunities, deliveries, pending grades, and resolved outcomes. Current pick/slip views may only present future, fresh rows with persisted engine output; missing confidence or movement must remain unavailable rather than use a synthetic default.

**Why:** Mixing these populations or manufacturing display values makes a healthy monitor appear unreliable and can present stale rows as actionable recommendations.

**How to apply:** Reuse persisted observations for report views, apply presentation-only freshness/actionability gates to current picks, and keep historical reconciliation independent from current pick display.

Provider and event/source identity must travel with current-pick intelligence and reasoning; player/sport/stat-only joins are unsafe when the same player has multiple provider lines or events.

**Why:** A player can have simultaneous provider-owned lines and event contexts, so a broad join can display evidence or narrative belonging to a different opportunity.

**How to apply:** Use provider plus source/event identity for current-row enrichment, and never infer the current opponent from the newest unrelated completed game.

Status readiness must distinguish missing real player-game evidence from a sport that has evidence but produced no qualifying directional opportunity in the reporting window; shadow-only or settlement rows are not readiness.

**Why:** A configured multi-sport slate can be healthy and fully scanned while only one sport has usable evidence. Collapsing that state into “no picks” hides provider coverage failures, while treating shadow data as evidence would misrepresent actionable readiness.

**How to apply:** Build readiness from source-level player-result coverage plus recent directional opportunity counts, and keep the report separate from delivery and market-observation totals.

Universal tracking grading is its own population: pending and resolved counts in
the tracking funnel must come from `PropTrackingLog`, not from directional
`PropOpportunityLog` rows.

**Why:** A healthy feed can have many tracked lines awaiting settlement while
having zero directional recommendations in the same window; using opportunity
grades makes the tracking funnel falsely appear empty.

**How to apply:** Keep directional pending/resolved metrics separate, but make
the primary tracking/grading display use the universal ledger and preserve
provider-level result counts.

Per-sport report tables must union normalized database maps with live FPR
telemetry and compatibility rows before rendering. Aggregate totals can be
correct while the table is empty if the renderer only trusts normalized
per-sport maps.

**Why:** Full-pool rescans persist their latest sport distribution in the
health sidecar, while normalized history/candidate aggregates can temporarily
have no per-sport rows after a restart or during database catch-up.

**How to apply:** Use FPR sport counts as the active/evaluation fallback for
the table, while preserving normalized observation, directional, and delivery
counts whenever those populations are available.