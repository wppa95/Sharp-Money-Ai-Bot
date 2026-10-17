# V4 pick-to-delivery verification

Verification date: 2026-10-17 UTC. Work was confined to the existing workspace.
No manual production inserts, grading, resets, backfills, migrations, deployment,
or GitHub push were performed. The running bot continued its normal database
writes and real Telegram deliveries. No synthetic picks or test sends were
injected into the running bot.

## Repairs

1. Removed the sender's same-reporting-day restriction and two-hour maximum.
   Qualified fresh picks can alert immediately. Known-pregame, started-event,
   freshness, source identity, selectable direction, qualification, and dedup
   checks remain. The generic timing utility still supports bounded windows for
   other callers; these two active platform delivery paths request no maximum.
2. Complete persisted PrizePicks batches now start the existing serialized peer
   evaluator immediately, rather than waiting for subsequent paced requests.
   There is still one active PrizePicks task, the existing processing lock,
   canonical handoff dedup, restart cancellation, and complete-batch persistence
   barrier. No new scheduler job or provider request was added.
3. Added separate factual `LINE CHANGE` notifications from complete persisted
   feeds for both platforms. Previous/current observations must have the same
   platform, source ID, event, player, sport, stat, and line classification.
   Source/event/transition dedup uses the existing audit table and unique index,
   not a migration. Notifications never write opportunities, mark PLAYs delivered,
   or alter an original pick's line or direction. New betting picks still pass
   the independent existing pick gates.
4. A short, verified optional-sport history sample is informational rather than a
   universal five-game blocker. Missing optional history does not create a
   direction; invalid provenance is still rejected. MLB/NBA/NFL/WNBA/NHL retain
   their existing six-window 75%/25% requirements. CFB remains history-optional;
   NFL does not. PrizePicks now invokes the same decision fallback as Underdog
   even when hit-rate windows are unavailable; that function may still return
   no direction.
5. Background history enrichment republishes the bounded durable history union
   for refreshed targets. A small remote refresh must not hide the larger sample
   already in SQLite from the cache-only live evaluator. Reads use bounded
   groups and add no remote calls.

## Observed lifecycle

Counts below are different populations, not interchangeable funnel denominators.
Raw response items can normalize to fewer canonical lines; stored totals include
history; repeated directional evaluations can update existing opportunity rows.

| Stage | Underdog | PrizePicks |
|---|---|---|
| OBSERVED / NORMALIZED | First post-restart native scan evaluated 13,342 own-feed props; quote time 04:01:14 | NBA response: 992 items, 871 canonical lines, committed by 04:05:54. NHL response: 2,440 items, 1,569 canonical lines by 04:09:12 |
| STORED | 48,447 source-owned history rows at 04:05:48 | 4,892 history rows at 04:09:39; the earlier 866 NBA rows were not reset |
| EVALUATION_ELIGIBLE / EVALUATED | Native evaluation logged 13,342 at 04:02:04; full shared branch finished 04:06:37 | All 871 NBA lines evaluated by 04:06:25 |
| EVIDENCE_CHECK / HISTORY | Ame's actual persisted evaluation used 13 stored DOTA games | Checked NBA example had 17 actual games; missing L20/L30 remained unavailable |
| ANALYZED / DIRECTIONAL | Ame persisted as OVER at the unchanged 105.6 line | NBA cycle produced 312 directional evaluations with source-aware opportunity/advisory records |
| QUALIFIED | Confirmed successful individual-pick audits, not the intermediate scan “qualified” counter | NBA cycle: zero actionable picks; direction alone does not waive the main-league rule |
| ALERT_CREATED / DELIVERY_ELIGIBLE / TELEGRAM_SENT | 102 successful ACTIONABLE_PICK audits from 04:02:25 to 04:09:37 | No successful delivery established during this observation window |

### Independence demonstrated live

- Underdog delivered individual picks before the PrizePicks NBA response arrived.
- PrizePicks NBA evaluation finished at 04:06:25, before the native Underdog
  branch finished at 04:06:37, before the later Underdog NBA response at 04:07:18,
  and before PrizePicks NHL ingestion completed at 04:09:12.
- Thus a complete batch no longer waits for the rest of the PropLine rotation.
  The shared quota's normal pacing remains, rather than being bypassed.

### Concrete PrizePicks stopping point

Victor Wembanyama, NBA points+rebounds, PrizePicks demon line 59.5,
source ID `129636:player_points_rebounds:Victor Wembanyama:demon`, event 129636:
the stored direction was UNDER. A read-only reconstruction from current stored
results found 17 games: L5 UNDER 5/5, L10 UNDER 10/10, L15 UNDER 14/15, season
UNDER 16/17, no pushes. L20 and L30 were unavailable. This cannot satisfy the
retained essential NBA six-window rule. No additional history, side availability,
or successful qualification was fabricated to send it.

### DOTA: normal execution, not only replay

Ame, Underdog Fantasy Points in Games 1+2, source ID
`b7a73e24-3511-41b7-9fb9-ed3fedd1a52a`, original line 105.6:

- The live snapshot associated with the 04:01:14 observation persisted OVER,
  `n_real=13`, L5=100%, L10=100%, with L20/L30 unavailable.
- Independent read-only calculation from the current 13 stored OpenDota-series
  rows agreed: L5 OVER 5/5, L10 OVER 10/10, season OVER 13/13.
- The real delivery audit records an Underdog individual-pick send at
  **04:02:42.464295**, at **105.6**, not a synthetic test alert.
- At 04:07:51 the normal history worker reported committed writes, including
  **100 DOTA result rows across 22 successful DOTA targets**, and durable-cache
  hydration for **2,609** refreshed targets. The worker persists completed
  results/cache data; monitoring owns persistence of evaluated snapshots.
- This verifies the normal evaluation/delivery path consumed the full stored
  sample. It does not assert an independently captured new OpenDota HTTP response
  or a new settlement for the upcoming event.

## Provider, database, and quota boundaries

Only normal worker requests were used; no extra manual live probe was issued.
Post-restart PropLine responses included complete empty MLB batches for each
platform and complete nonempty NBA/NHL batches. Empty slates retained their
existing bounded backoff.

Both primary platforms remain in `bot/data/sharp_money.db`, with distinct
provider/source IDs and source lines. PropLine still uses its existing persistent
shared operational quota ledger, not a separate picks database. A read-only
post-restart snapshot showed 69 reservations for the current daily budget and
36 in the rolling hour: restart did not reset usage. Target 960/day, hard cap
1,000/day, hourly limit 40 and global pacing were not changed or stress-tested
against the live provider.

## Verification and limitations

- Focused final run: **234 passed**, 19.21 seconds, across the nine modules below.
- Initial full run: **5,425 passed, 5 failed**. Four tests still expected the
  explicitly superseded timing restrictions; one expected a universal five-game
  floor for history-optional CFB. Their assertions and mocked transport were
  updated to the requested policy; NFL evidence rejection was retained.
- Final recovered full-suite run: **5,430 passed**, 418.27 seconds.
- An unexpected workspace restart interrupted the next full-suite attempt and
  discarded its temporary log. That run is not counted as passing. Recovery
  validation writes its log to the ignored workspace path
  `.local/v4-pick-delivery-full-final.log`. The restart's cause is not established;
  the replacement container's memory-event counters reported no OOM event.
- `python -m compileall -q bot`: passed.
- `git diff --check`: passed, including the final report.
- Managed bot workflow was restarted after the code batch; exactly one
  `python bot/main.py` process was verified. Screenshot of port 8099 `/health`
  returned `OK`. The later workspace interruption required service recovery.
  Recovery completed at 04:22 UTC: API server and component-preview workflows
  are running, the bot has one process (PID 1002), and its scheduler and Telegram
  application both logged successful startup at 04:22:08. A fresh health
  screenshot again returned `OK`.
- LINE CHANGE formatting, transition dedup across restart, source/event/variant
  isolation, coalescing, reversal cancellation, and original-pick immutability
  were verified in automated tests. Any live movement notification claim must
  be supported by a real audit; none was fabricated.
- The notification worker is bounded to one task, 1,000 pending identities and
  40,000 retained observations. Pending moves coalesce to the latest verified
  line; reversals back to the original line cancel. Overflow logs a warning.
  Its first observation after restart primes the cache without inventing a move.
- Missing history in many configured markets remains a coverage gap. The normal
  worker reported empty-result targets across CS, DOTA, FIFA, KBO, LOL, MMA,
  motorsports, NBA, football, tennis, VAL, WNBA, NHL and MLB. This is not evidence
  that every market in those sports is unsupported, nor a promise of coverage.
- PrizePicks NHL ingestion was confirmed, and NHL directional opportunity writes
  were observed, but that batch's completion/delivery was not established before
  the workspace interruption. Do not infer that all its lines were disqualified
  or that a PrizePicks delivery succeeded.
- No thresholds unrelated to history optionality were lowered. Existing grading,
  MLB mappings/thresholds, equal platform policy, adapter binding, API budget,
  scheduler ownership, and individual-pick-only behavior remain intact.
- Footer retained exactly:
  `Monitoring player props across multiple sports. Not betting advice.`

### Commands

```sh
python -m pytest \
  bot/tests/test_v4_pick_delivery_followup.py \
  bot/tests/test_prizepicks_independent.py \
  bot/tests/test_v4_equal_platform_followup.py \
  bot/tests/test_player_history_job.py \
  bot/tests/test_timing_filter.py \
  bot/tests/test_telegram_funnel_delivery_gate.py \
  bot/tests/test_alert_limits.py \
  bot/tests/test_pregame_opportunity_lifecycle.py \
  bot/tests/test_propline_cs_priority.py -q
python -m pytest bot/tests -q
python -m compileall -q bot
git diff --check
```

### Exact changed files

Application:
- `bot/alerts.py` — immediate safe delivery without countdown/calendar waits.
- `bot/engine/timing.py` — optional maximum timing horizon.
- `bot/market_engine.py` — immediate serialized handoff, shared decision fallback,
  persisted-batch notification hooks and shutdown cleanup.
- `bot/engine/line_change_notifications.py` — bounded independent factual notices.
- `bot/database.py` — notification dedup through the existing audit index.
- `bot/engine/delivery_evidence.py` — short verified optional history.
- `bot/player_history_job.py` — bounded durable-history cache hydration.

Tests:
- `bot/tests/test_v4_pick_delivery_followup.py` — 21 new regression cases.
- `bot/tests/test_alert_limits.py` — retained evidence gate without calendar wait.
- `bot/tests/test_pregame_opportunity_lifecycle.py` — immediate timing expectations.
- `bot/tests/test_propline_cs_priority.py` — optional CFB versus essential NFL history.

Policy/reporting:
- `.agents/memory/MEMORY.md`
- `.agents/memory/pregame-safety-boundary.md`
- `.agents/memory/two-layer-alert-architecture.md`
- `.agents/memory/market-signal-bypass.md`
- `.agents/memory/live-monitoring-db-boundary.md`
- `.agents/memory/workflow-process-verification.md`
- `docs/v4-pick-delivery-verification.md`

The uploaded instruction file is user-supplied, not an implementation change.
