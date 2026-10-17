# V4 audit, targeted repairs, and verification

## Scope and conclusion

Inspection and changes performed on October 17, 2026 UTC (October 16 in
America/Chicago). This report covers the Replit workspace, not Railway.

The latest uploaded specification explicitly makes **PrizePicks primary and
Underdog secondary**. That supersedes the earlier equal-primary instruction.
Both feeds, source-owned records, individual alerts, and shared evidence remain.

Concrete reporting, command-selection, ingestion-handoff, history-identity,
grading-safety, and committed-write telemetry defects were repaired. This is
**not a claim of verified live PrizePicks ingestion or verified fresh PropLine
settlements**. The bounded live probe returned no response. No fictitious
provider result or recommendation was inserted to satisfy an acceptance check.

## Demonstrated defects and repairs

1. `/picks` fetched PrizePicks rows and discarded them, and its selection helper
   intentionally ignored the PrizePicks argument. Both now retain independent
   platform rows, directions, source IDs, events, and original lines.
2. Status, tracking, statistics, feed-count, and dashboard presentation hid
   PrizePicks. Both platforms are visible, with primary/secondary roles and
   separately scoped populations. Configuration is not presented as proof of a
   successful provider fetch. Feed diagnostics distinguish ok, empty, and failed.
3. `/analyst` required a current Underdog snapshot and filtered historical
   deliveries by upcoming-game state. It now prefers confirmed durable
   opportunity/audit records, including PrizePicks and past events. It reads
   the actual opportunity schema's `recommendation`, not a snapshot-only field.
   It does not manufacture an absent model assessment or sample size.
4. A moved-line delivery could inherit the mutable opportunity's current
   direction/result. Historical display now uses the immutable audit line and
   available direction snapshot; verified actuals are compared to that line.
   An older moved-line audit with no recoverable original direction remains
   unknown rather than borrowing today's direction.
5. `/picks` used OVER numerators for UNDER histories. Shared strict directional
   counts now drive its windows and H2H presentation. Pushes are not UNDER wins;
   missing UNDER counts are not calculated as `1 - OVER rate`. A comparison
   line cannot overwrite the selected row's original source line.
6. Generic PropLine stat normalization collapsed map/game ranges into bare
   kills/headshots/assists/fantasy points. Explicit ranges are preserved.
   CS raw fields match only the same documented segment. CS games 1+2 aliases
   can use the documented maps 1+2 endpoint; maps 1+2+3 cannot use that endpoint.
7. CS2/CS and Dota2/DOTA identities diverged across discovery, canonical
   observations, and completed-history lookups. Esports aliases now normalize
   at those boundaries; bulk history reads retain requested lookup keys.
   Legacy aliases remain readable without rewriting stored records.
8. Composite grading selected each stat component independently by date.
   Both single and bulk paths now reject mixed, unknown, conflicting-opponent,
   or non-original event components. The single-result path also no longer
   falls back to a different event merely because player/stat/date match.
   This is a demonstrated shared MLB data-correctness fix, not a change to MLB
   scoring, markets, or qualification thresholds.
9. A failed sport could erase already persisted, complete PrizePicks batches
   from another sport. Complete handoffs are retained and evaluated even if
   another feed fails. Incomplete batches remain withheld. Provider failures
   now reach health diagnostics.
10. The global last-write marker missed normal history/grading writes. The
    common SQLite session wrapper updates it after successful mutating commits,
    including ORM autoflush, not SELECTs, rollbacks, failed commits, or zero-row
    SQL updates.
11. The requested **By current sport** and **Recently Tracked Props** panels
    were removed from `/dashboard`. Stored history was not deleted.

## Real data and runtime checks

### Database observations

Read-only SQLite inspection used `mode=ro`, bounded queries, and short query
deadlines against `bot/data/sharp_money.db`. No reset, backfill, or manual
production-data update was performed.

At the inspected pre-restart snapshot:

- `prop_line_history`: 46,245 Underdog rows; latest observation
  `2026-10-17 02:55:26.458970` UTC.
- **No PrizePicks rows** in that table. Therefore live PrizePicks database
  ingestion is not certified by this run.
- A bounded last-3,000-opportunity sample contained directional Underdog rows
  across MLB, NBA, NFL, NHL, WNBA, CFB, CS, DOTA, LOL, and tennis, all pending.
  This sample is not an all-time grade count or a claim that every row qualifies.
- The latest 3,000 completed-history rows were NHL records from
  `nhl_stats_api`, through October 15.
- A bounded last-1,000 delivery-audit sample contained Underdog sends. The
  latest was `2026-10-17 01:27:54.165063`. This is not proof of current picks,
  all-time recipients, or PrizePicks delivery.
- The existing evidence table contained 51 event-result cache records, 103
  player-game cache records, and 115 player-history cache records. The inspected
  result/history samples were older, not fresh evidence for unrelated events.

These figures are snapshot populations with different scopes, not numbers
that should be added together.

### Non-MLB history examples

Real stored rows were replayed through `_fetch_and_compute_hit_rates` with
remote access disabled, once for each platform identity. No recommendation,
grade, or history record was written during this replay.

| Player / market / original line | Available stored history | Replay result |
|---|---|---|
| Connor Bedard / NHL shots on goal / 2.5 | 30 rows loaded, `nhl_stats_api`, latest October 15 | Real history for both source identities; UNDER L5 2/5, L10 4/10. Existing stored evaluation independently showed OVER L5 3/5. |
| Ame / DOTA fantasy points in games 1+2 / 105.6 | 12 `opendota_series` rows, latest October 16 | Real history for both source identities; UNDER L5 0/5, L10 0/10. |
| sjuush / CS kills on maps 1+2 / 26.5 | No exact stored game rows | L5/L10 unavailable for both identities; no estimated rates. |
| Alexander Zverev / tennis games played / 23.5 | No exact stored game rows | L5/L10 unavailable for both identities; no estimated rates. |

This demonstrates that the loader accepts existing history without a PropLine
result and marks missing history insufficient. It is not a claim that the
read-only replay created a qualified PrizePicks pick.

The inspected DOTA snapshot still carried an older missing-history reason even
though stored results were available. The replay accepted those rows. Automatic
refresh of that specific snapshot was **not independently verified**; its old
reason must not be described as a current successful evaluation.

### Provider response checks and limitations

- Existing native Underdog observations and runtime fetch telemetry were
  inspected. After restart, a fresh Underdog fetch was recorded at
  `2026-10-17 03:06:34` UTC with zero provider error streak.
- Public PropLine documentation was fetched from
  `https://prop-line.com/llms-full.txt`; the response identified an October 16,
  2026 update. This was a public documentation request, not a paid sports-data
  result request.
- One NHL/PrizePicks probe used the existing `PropLinePrizePicksProvider`,
  global persistent budget, and pacing, with a 30-second bound and no event
  fan-out. It timed out without a response. No HTTP status, raw successful
  envelope, complete slate, or normalized live PrizePicks count was established.
- That one-off diagnostic command also exited with a cleanup `AttributeError`
  because `PropLineClient` has no `close()` method. This was a diagnostic script
  mistake, not a demonstrated bot ingestion failure. The script exited; it
  was not installed in the app. No second probe was made to hide the failure.
- No suitably matched fresh PropLine completed result was independently
  verified for either platform. Older cache data was not forced against
  unrelated picks. Live HIT/MISS/PUSH correctness for an actual pair of
  platform-owned lines therefore remains unverified.

### Shared quota and database

The pick/evidence database remains the existing central SQLite database.
`bot/data/propline_quota.sqlite3` is the pre-existing shared operational quota
ledger, not a new platform-specific pick database.

- Code retains one persistent request budget across ingestion, history,
  evidence, results, and settlement consumers.
- Normal target: 960/day; hard configured maximum: 1,000/day; normal reserve:
  40; shared hourly ceiling/pace: 40.
- Feed and read-only endpoint clients use the same default budget object and
  ledger. A resolved-export sub-limit is an additional restriction, not a
  separate allowance bypassing the shared API budget.
- Observed October 17 daily count was 29 before restart and 36 afterward.
  The later rolling-hour ledger held 14 reservations. These are live snapshots,
  not an allocation of every increase to the diagnostic script.
- The daily counter did not reset with the bot restart. No deliberate
  exhaustion test was run against the paid API.
- Concurrent/restarted clients, caching, retries, shared pacing, incomplete
  snapshots, and budget accounting are covered by the existing automated tests.

### Startup and operational health

The managed **Sharp Money Bot** workflow was restarted, with a single
`python bot/main.py` process verified afterward. Startup initialized the
existing database, registered both providers, started Telegram polling and
the existing scheduler jobs. Discord was reported unconfigured.

The browser check of `http://127.0.0.1:8099/health` returned **OK**. The browser
also reported a non-page-resource 404; the health response itself rendered OK.
No interactive Telegram command UI session was performed.

The actual health sidecar subsequently recorded:
`last_db_write = 2026-10-17 03:07:26 UTC`, operation `database_commit`.
The new monitoring cycle had not yet been certified complete at that snapshot.
Job execution/last-start/last-run counters are not themselves proof of graded
rows; delivery, observation, result, and committed-write counters remain distinct.

No current Telegram Conflict or Discord GeneratorExit was reproduced. The
startup's historical `unexpected_exit` classification is not proof of a new
runtime crash or a Railway failure.

## Exact changed files

### Runtime

- `bot/commands.py`: platform roles and visible source counts; source-aware
  current picks; original source lines; correct UNDER windows; durable analyst
  records; no fake unavailable intelligence block.
- `bot/database.py`: commit telemetry; esports history aliases; stricter
  same-event grading; immutable delivered line/direction/outcome display.
- `bot/engine/dashboard.py`: separate platform populations and requested panel
  removals; explicit count scopes.
- `bot/engine/offering_coverage.py`: canonical aliases in coverage reports.
- `bot/engine/player_props_pipeline.py`: esports aliases at observation intake.
- `bot/engine/player_results.py`: shared strict directional-window counts.
- `bot/engine/sport_policy.py`: central esports aliases and legacy read aliases.
- `bot/market_engine.py`: PrizePicks-first bookmaker traversal, complete-batch
  retention, independent handoff after partial failure, provider diagnostics.
- `bot/providers/propline.py`: retain map/game segments and only documented
  CS market equivalences.

### Tests

- `bot/tests/test_v4_audit_repairs.py`: new regression cases.
- `bot/tests/test_dashboard.py`: new visible-provider/removed-panel contract.
- `bot/tests/test_dashboard_upgrade.py`: FPR fallback without removed panels.
- `bot/tests/test_delivery_result_and_prizepicks_health.py`: visible feed
  populations and separate failed/empty states.
- `bot/tests/test_prizepicks_independent.py`: preserve good complete batches
  while withholding a failed batch.
- `bot/tests/test_telegram_command_reliability_display.py`: primary/secondary
  roles and both provider populations, including timeout-resilient reports.
- `bot/tests/test_v32_final_targeted_fix.py`: requested removed-panel contract.
- `bot/tests/test_v35_cold_start_lifecycle.py`: same removed-panel contract.

### Documentation / durable policy

- `.agents/memory/MEMORY.md`
- `.agents/memory/connector-architecture.md`
- `.agents/memory/provider-scope-separation.md`
- `.agents/memory/source-priority.md`

Those four files replace obsolete equal-primary instructions with the user's
latest explicit policy. No private user profile was changed.

- `docs/v4-audit-repair-verification.md`: this report.

The uploaded instruction file was read, not edited.

## Tests and compilation

Actual final validation:

- `python -m pytest bot/tests -q --tb=short`: **5,399 passed**, zero
  failures/skips, in 391.29 seconds. Output:
  `/tmp/v4-audit-final-suite.log`.
- After the final feed-status wording added explicit empty/failed labels:
  `python -m pytest bot/tests/test_delivery_result_and_prizepicks_health.py bot/tests/test_v4_audit_repairs.py -q --tb=short`:
  **32 passed**, zero failures/skips, in 8.54 seconds. Output:
  `/tmp/v4-final-feed-states.log`.
- `python -m compileall -q bot`: passed.
- `git diff --check`: passed.

No test was skipped to obtain these results. Live-only checks listed as
unverified below are not counted as passing automated provider checks.

New regression module: 15 test functions / 24 parameterized cases covering
platform reporting, removed panels, aliases, canonical analyst schema,
tracking, actual directional windows/pushes, exact CS map segments, original
event composites, alias-aware persistence, commit/rollback/failure/autoflush
telemetry, immutable audit direction/line/outcome, partial-feed handoffs, and
independent current platform picks.

Earlier diagnostic runs intentionally exposed old behavior, then verified
repairs. One intermediate full run had 17 failures / 5,378 passes. Some
source-inspection failures in that run came from editing while Python had
already imported the old line positions; later focused runs on unchanged files
passed those checks without weakening their funnel assertions. Other failures
were obsolete hidden-PrizePicks/removed-dashboard expectations and the missing
CS games-1+2 mapping; these were corrected.

## Remaining limitations and unsupported coverage

- Live PrizePicks ingestion, current source-selectable sides in its own live
  response, individual live PrizePicks delivery, and fresh PropLine grading
  for either platform remain unverified.
- Missing provider data is not proof that all markets or sports are supported.
  Exact CS history and the sampled tennis market had no stored game results.
- DOTA has an OpenDota route but no documented PropLine player-market mapping
  in the current adapter. A supported feed name does not establish a graded
  player-market endpoint.
- KBO/NPB have no existing public `PlayerStatsProvider` route; table tennis and
  badminton match/set results do not establish detailed player-stat support.
- Cross-provider event IDs still need sufficient exact identity/team/opponent
  evidence. Ambiguous or context-poor rows remain pending. This repair does not
  make date-only matching a universal settlement fallback.
- Automatic refresh of the sampled DOTA evaluation and a completed fresh
  post-restart full monitoring cycle were not certified by the snapshot.
- The changes do not promise a pick for every sport, current line, or cycle.

## Acceptance checklist

| Item | Verified scope | Live / remaining limitation |
|---|---|---|
| A — ingestion | Distinct platform rows and complete-batch handoffs covered by regressions; native Underdog active | Live PrizePicks ingestion not established |
| B — history | Real NHL/DOTA DB-row replay for either platform; absent CS/tennis history stays insufficient; scoped normalization regression | Sampled DOTA snapshot auto-refresh unverified |
| C — recommendation/gates | Independent original-platform directions retained; scoring/qualification thresholds unchanged; no slip generation added | No new live qualified PrizePicks case |
| D — platform/direction | Original lines kept; source identities distinct; UNDER/push arithmetic corrected; existing exact-side offering checks preserved | Fresh PrizePicks selectable-side response unavailable |
| E — delivery/analyst | Confirmed audit linkage, historical source-line/direction replay, dedup tests; exact existing footer retained | No deliberate live betting alert sent for testing |
| F — grading | Same-event checks and existing HIT/MISS/PUSH/VOID/CANCELLED/pending/idempotency regressions | No fresh exact-match live PropLine result pair |
| G — reports/telemetry | Distinct populations, PP visibility, requested removals, direction-correct windows, actual committed-write marker | Generic job completion is not a fabricated work count |
| H — regressions | MLB shared safety cases, existing sport/date/scheduler/quota/individual-only tests | Not a live certification of every sport/provider |

The retained active alert footer is:

> Monitoring player props across multiple sports. Not betting advice.

No Odds API ingestion, new provider, scoring-threshold change, new migration,
database reset, multi-pick generation, or Railway deployment was introduced.
Existing startup schema checks still run idempotently; that does not constitute
a new migration.

## Railway checklist — user-controlled, not performed

1. Review these changes in Replit and commit/push the reviewed revision to the
   intended GitHub branch. Confirm Railway's connected repository, branch, and
   build revision; do not assume it matches this workspace.
2. Preserve the existing Python dependencies and bot start command
   `python bot/main.py`. Configure only one active polling process per Telegram
   bot credential; independently running Replit and Railway pollers can conflict.
3. Set the existing `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and/or
   `ALLOWED_USER_IDS`, plus `PROPLINE_API_KEY`, in Railway's secure variables.
   Never paste their values in code, this report, or GitHub.
4. Review `PROPLINE_API_URL` against the authorized endpoint. Retain
   `PROPLINE_BOOKMAKERS=prizepicks,underdog` and `UNDERDOG_ENABLED=true` unless
   intentionally changing the specified platform scope. Retain existing
   `PLAYER_PROP_SPORTS`, `ACTIVE_SPORTS`, `UD_ALERT_SPORTS`,
   `ALERT_DISABLED_SPORTS`, and `REPORTING_TIMEZONE` settings; do not lower gates
   merely to produce picks.
5. Point `BOT_DATABASE_URL` at the existing central SQLite file on persistent
   storage. Keep the existing quota/rotation ledger persistent too:
   `PROPLINE_QUOTA_LEDGER_PATH`, and `PROPLINE_ROTATION_STATE_PATH` if explicitly
   configured. Do not create a separate PrizePicks pick database or copy the
   development database over production.
6. Ensure `BOT_HEALTH_PORT` or Railway's existing `PORT` reaches `/health`.
   Preserve the current deployment's volume mount and writable SQLite/WAL
   directory; these Railway values were not inspected or changed here.
7. After a user-approved Railway release, inspect startup, one active poller,
   shared quota continuity, independent feed outcomes, actual parsed PP rows,
   exact-side availability, source-specific original lines, and completed-game
   identity before certifying live results. Recheck `/health`, `/status`,
   `/funnel`, `/tracking`, `/picks`, `/alerts`, `/analyst`, and `/dashboard`.
8. Keep The Odds API retired. Do not enable multi-pick jobs or commands.

No Railway action or GitHub push was performed by this task.
