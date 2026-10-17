# V4 equal-platform follow-up

Verification date: October 17, 2026 UTC. Scope: current Replit workspace only.
No GitHub push, Railway deployment, production database inspection, destructive
backfill, provider addition, new migration, or scoring-threshold change.

## Code changes

- **Equal primary platforms:** PrizePicks and Underdog have the same primary
  status in the four durable policy documents, health/configuration labels,
  dashboard roles, and scope/provider documentation. Neither is a fallback for
  the other. Original IDs, events, directions, and lines remain source-owned.
- **Neutral request order:** Removed the hardcoded PrizePicks-first sort.
  Shared PropLine scans alternate the first configured bookmaker across
  cycles, using the same quota, locks, empty-slate cooldowns, and sport rotation.
  A platform's designation does not determine request priority.
- **Comparison neutrality:** The legacy directionless `best_provider` field
  now describes the numerically lowest available line for OVER context,
  rather than preferring PrizePicks by name. UNDER-friendly comparisons remain
  separate. The source-owned pick itself is not changed by a comparison.
  `PROVIDER_ORDER` remains a stable presentation order, not a delivery priority.
- **Actual ingestion root cause:** Startup constructed a temporary
  `PropLinePrizePicksProvider` only to check configuration. The module-level
  `_propline_provider` used by `propline_request_scan_job` remained `None`.
  That worker returned `False` before discovery or a network call. Startup now
  binds and checks the same adapter instance actually used by the worker,
  after resetting disposable runtime state.
- Preserved source-aware `/picks`, historical `/analyst`, original audit
  lines/directions, strict UNDER counts, same-event grading, aliases,
  complete-batch retention, committed-write telemetry, dashboard removals,
  supported-sport configuration, MLB thresholds, and individual-only delivery.

## Live checks actually completed

One explicitly bounded live diagnostic used the configured authorized adapter,
its normal shared persistent request counter, and normal limiter. It did not
write picks/results or send an alert. It allowed at most one actual HTTP request:

| Stage | Actual observation |
|---|---|
| Configuration | Authorized host `api.prop-line.com`; configured; default HTTP timeout 15 seconds |
| Shared pacing | Reported wait of 20.59 seconds; HTTP began at elapsed 21.06 seconds |
| Endpoint | `/v1/sports/hockey_nhl/odds`, bookmaker PrizePicks, no event fan-out |
| HTTP | **200**, received at elapsed 25.71 seconds |
| Body | JSON list, parsed by the existing adapter |
| Normalized result | `succeeded=true`, `complete=true`, `from_cache=false`; **2,371 raw and 2,371 normalized records** |
| Diagnostic persistence | None; this probe was not the scheduled ingestion worker |

This disproves a current blanket networking/endpoint failure. The shared queue
can consume much of an outer 30-second deadline before the separate HTTP
timeout even starts. The earlier probe had no stage instrumentation, so its
exact timeout cause cannot be established retrospectively. Do not describe it
as a proven provider outage or a proven quota timeout.

The manual probe did not bypass or reset the ledger. It used the existing
960/day operating target, 1,000/day hard maximum, and 40/hour shared pace.
No additional requests were made to force a fresh result or consume the reserve.

The Replit bot was restarted after the code batch. One process was confirmed;
startup and Telegram application startup completed. Normal scheduled ingestion
was observed separately from the successful manual probe. Its final state is
recorded in the validation results below.

## Independent stored observations

The previous report's `prop_line_history` observation did **not** mean that
PrizePicks had never been stored:

- The universal tracking ledger contained **32,065 PrizePicks observations**
  and **283,112 Underdog observations** at the inspected snapshot.
- The latest old PrizePicks tracking observation was September 28, 2026,
  19:33:54 UTC. Line history has a separate retention/pruning policy; it is
  not the same all-time population as universal tracking.
- At an early post-restart snapshot, before a productive PrizePicks batch,
  `prop_line_history` contained
  **47,025 Underdog rows and no PrizePicks rows**, with latest Underdog
  observation October 17, 03:32:07 UTC.

The normal worker subsequently provided that separate persistence proof:

| Central database population | Actual inspected count | Latest timestamp UTC |
|---|---:|---|
| PrizePicks `prop_line_history` | **866** | October 17, 03:36:41 |
| Underdog `prop_line_history` | **47,293** | October 17, 03:38:12 |
| New PrizePicks tracking records since restart | **866** | October 17, 03:36:49 |

The worker logged **987 NBA observations**, **273 queued player-history targets**,
and **866 canonical pending handoff entries**. The canonicalized count is not
the raw observation count. Inspected PrizePicks rows retained their own IDs,
NBA event IDs, event times, original lines and standard/goblin variants, and
`provider=PrizePicks`. Underdog observations continued separately.

Before that productive batch, the normal worker reported complete empty MLB
responses for both platforms and applied their separate 15-minute empty-slate
cooldowns. Empty MLB was not treated as missing all-sports provider coverage.

Both platforms therefore have **independent current stored observations**,
not merely historical records or an isolated successful HTTP probe.
At inspection, the sampled fresh PrizePicks rows had no stored direction yet;
full post-ingestion evaluation, qualification, and a live PrizePicks delivery
were not independently certified. The shared ingestion task was still active
at 03:37:43 while Underdog completed its scan. No synthetic direction or alert
was created to close that gap.

## History, grading, and the “zero resolved” discrepancy

### Real stored DOTA evaluation replay

A read-only SQLAlchemy connection (`mode=ro`) used the actual application history
queries, bulk-compatible rows, snapshot builder, validation, scorer, and
decision function. Remote fetching was disabled and no evaluation was persisted.

For Ame, DOTA, **Fantasy Points in Games 1+2**, original line **105.6**:

- 13 stored `opendota_series` games were available through October 17.
- The current recomputation returned **OVER** with 13 real games and supporting
  data, for either platform identity.
- OVER L5: **4/5**; UNDER L5: **1/5**.
- OVER L10: **9/10**; UNDER L10: **1/10**.
- Season OVER: **12/13**. L20/L30 remain unavailable, not projected from 13.
- The previously inspected Underdog snapshot had only two real games in its
  serialized validation and a missing-history reason. That stored snapshot is
  not evidence that the current loader rejects the 13 available games.

The recomputation proves the available history can refresh the evaluation
without PropLine. It does **not** certify that the asynchronous full-pool worker
has already written that refreshed snapshot or sent a pick.

Known-result tests cover OVER/UNDER L5/L10/L20/L30, aggregate H2H and H2H
L5/L10/L20, pushes, and missing/short history. Pushes remain completed samples
but never directional wins. No complement-derived UNDER estimates were added.

### Actual populations

The universal tracking ledger was not globally unresolved:

- **5,914 HIT, 9,196 MISS, 4 PUSH, 80 VOID**, alongside pending observations.
- Verified provider-specific counts included:
  - Underdog: **5,773 HIT, 9,001 MISS**.
  - PrizePicks: **107 HIT, 148 MISS, 4 PUSH**.
- These all-time tracking totals include historical non-pick'em sources, so
  they must not be mislabeled as current delivered-pick performance.
- The latest 1,000 directional opportunity records were Underdog and pending,
  for events between October 17 and October 22. They are not the all-time
  tracking population or completed player-game evidence.
- An exact raw player/stat/sport/event-ID join from the latest 1,000 pending
  tracking rows to stored player-game results returned no matches. This bounded
  check does not replace the application's stricter validated cross-ID matching.
- Some provider-by-result aggregates exceeded the short read-only query deadline.
  They were stopped and were not reported as zero.

Completed history, neutral tracked observations, directional opportunities,
delivered picks, and recipient sends are different populations. A completed
history row does not imply a corresponding observed original line, a directional
recommendation, a delivered bet, or sufficient exact event identity.

### Fresh available completed result — not forced into a grade

Existing runtime evidence included a fresh PropLine `player_games` cache record
fetched October 17 at 03:22:16 UTC for Austin Reaves, NBA:

- Event **338618**, commenced October 14 at 02:00 UTC.
- Explicit provider status **final**.
- Los Angeles Lakers versus Golden State Warriors.
- Numeric rebounds result **4.0**.
- The same result was present in central player history as
  `propline_player_games`, date October 14, event 338618.
- There was **no exact original tracked rebounds observation** for that player
  and event. Therefore this result was not forced onto another event or line.

No fresh exact-match platform-owned pick was independently observed receiving
a new HIT/MISS/PUSH grade during this follow-up. Existing resolved rows are not
retroactive proof of a newly verified provider settlement. Exact-match grading,
source-owned line semantics, ambiguous/pending behavior, and idempotence are
covered by the focused/full regression suites.

## Validation results

- Focused suite before restart: **278 passed** in 26.20 seconds.
- New regression module: 10 cases covering worker binding, alternating peers,
  timeout, invalid JSON, empty response, complete successful persistence,
  both-direction complete windows/H2H, and DOTA refresh for either platform.
- `python -m pytest bot/tests -q --tb=short`: **5,409 passed**, zero failures
  or skips, in **396.49 seconds**. Output:
  `/tmp/v4-followup-full-suite.log`.
- `python -m compileall -q bot`: passed.
- `git diff --check`: passed.
- Post-restart: one `python bot/main.py` process, application started, health
  listener on 8099, browser `/health` rendered **OK**. The browser's separate
  missing-resource 404 did not affect the health response.
- Full-suite results do not count the unmatched fresh result, unobserved
  PrizePicks evaluation/delivery, or automatic DOTA snapshot persistence as
  passing live checks.
- Earlier focused debugging runs had four test-call signature errors, then one
  fixture stat-case mismatch and one obsolete fixed-order expectation. These
  were corrected; they were not live provider failures.

## Changed files and risks

Runtime:

1. `bot/main.py` — initialize the actual shared-feed worker adapter.
2. `bot/market_engine.py` — idempotent startup initializer; neutral alternating
   bookmaker order, reset with disposable runtime state.
3. `bot/commands.py` — equal-primary user-facing roles.
4. `bot/engine/dashboard.py` — equal-primary roles.
5. `bot/alert_scope_filter.py` — equal-primary policy label/comments.
6. `bot/engine/player_prop_market.py` — neutral comparison selection and comments.
7. `bot/providers/underdog_provider.py` — remove reference/fallback-only policy.

Tests:

8. `bot/tests/test_v4_equal_platform_followup.py` — new targeted regressions.
9. `bot/tests/test_v4_audit_repairs.py` — equal-primary assertion.
10. `bot/tests/test_telegram_command_reliability_display.py` — equal-primary labels.
11. `bot/tests/test_prizepicks_independent.py` — require every configured source
    without enforcing a permanently preferred first platform.
12. `bot/tests/test_player_prop_market.py` — numerical rather than named-platform
    comparison expectation.

Policy/report documentation:

13. `.agents/memory/MEMORY.md`
14. `.agents/memory/connector-architecture.md`
15. `.agents/memory/provider-scope-separation.md`
16. `.agents/memory/source-priority.md`
17. `docs/v4-audit-repair-verification.md` — correct the prior report's policy.
18. `docs/v4-equal-platform-followup.md` — this report.

Operational risks: the previously inactive worker now competes for the existing
shared quota and SQLite write capacity, without an increased allowance. Request
order alternation resets on a normal runtime reset, but daily quota does not.
The legacy directionless comparison field now means OVER-friendly numerical
line; original pick lines and separate UNDER comparisons are unchanged.

Two separate optional follow-ups were proposed, not implemented here:
record successful empty shared-feed responses in the health feed-count panel
(the inspected panel had no per-sport entry despite the worker's successful
empty-response logs), and hand complete persisted batches to evaluation before
later paced requests finish. Neither proposal changes the verified current
database counts or represents a claim that live qualification already passed.

No claim is made of universal sport/market history availability. The sampled
CS/tennis history gaps and unsupported detailed markets remain gaps unless a
valid source supplies the required data.
