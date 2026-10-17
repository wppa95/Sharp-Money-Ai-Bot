# V4 final policy verification

## Implemented scope

- PrizePicks and Underdog remain equal primary platforms, with separate source
  identities, selections, original lines, observations, and audit records.
- Actionable picks must be for the current **America/Chicago** calendar day.
  There is no two-hour delivery maximum: qualifying same-day picks send
  immediately. Tomorrow's picks remain blocked until Central midnight.
- Existing started-event, freshness, source, selectable-side, deduplication,
  serialized batch evaluation and complete-persistence protections remain.
- MLB/NFL require **at least six verified completed games**. Five fails.
  Historical-window completeness and the previous six-window cutoffs no longer
  gate qualification. Other sports have optional history, but still need a
  supported direction. Existing direction-rate and contradiction thresholds
  were not lowered. Short actual history can use an existing real window rather
  than being universally rejected below five samples.
- Missing windows remain missing. Movement does not manufacture a direction.
- Initial delivery, LINE CHANGE and GAME REMINDER have independent purposes.
  Neither operational notice creates a new qualified pick or changes a PLAY.
- Individual-pick-only behavior, existing providers and shared PropLine quotas
  remain unchanged. The Odds API was not enabled.
- Footer remains exactly:
  `Monitoring player props across multiple sports. Not betting advice.`

## One-hour discrepancy: demonstrated cause and time path

The active multi-platform formatter explicitly used `America/New_York` and
an `ET` label despite the reporting configuration defaulting to Chicago.
On ordinary CDT/CST dates, the same UTC instant therefore appeared one hour
later than Missouri local time; DST-boundary cases are tested separately.
The PrizePicks compatibility formatter also displayed raw UTC rather
than the configured local time.

Both alert formatters now use the shared reporting conversion.
Reporting configuration is America/Chicago, not a fixed UTC-5 offset.

The existing Underdog `_parse_event_time` and PropLine `_parse_datetime`
normalize explicit UTC/offset timestamps to UTC before dropping timezone
information for the existing SQLite convention. Those stored naive values are
UTC, **not Central Time**. The shared display/day converter attaches UTC once
and converts to Chicago. Provider parsing/storage conventions were not changed.

Tests pass real parser functions the following **controlled timestamps**:

| Input instant | Expected display |
|---|---|
| 2026-07-15 19:00Z | Jul 15, 2:00 PM CDT |
| 2026-01-15 20:00Z | Jan 15, 2:00 PM CST |
| 2026-07-15 15:00-04:00 | Jul 15, 2:00 PM CDT |
| 2026-03-08 07:59Z | Mar 8, 1:59 AM CST |
| 2026-03-08 08:00Z | Mar 8, 3:00 AM CDT |
| 2026-11-01 06:30Z | Nov 1, 1:30 AM CDT |
| 2026-11-01 07:30Z | Nov 1, 1:30 AM CST |

Reporting-day tests verify 23-hour/25-hour DST days and the exact Chicago
midnight boundary. No blanket subtraction of one hour was added.

The specific previously reported "3 PM instead of 2 PM" live event was not
identified in the request, so that individual provider event was not
independently verified. The formatter's one-hour zone mismatch is directly
demonstrated by code and the parser-to-display regression tests.

## Reminder implementation and evidence

- A successful authoritative PLAY and its separate reminder state commit in
  **one transaction in the existing central SQLite delivery-audit table**.
  No new table, migration, database, per-player request or per-pick timer.
- Due time is the verified UTC event instant minus two elapsed hours. Display
  uses Central Time. This remains correct over DST: a 1:30 AM CST fall-back
  game has a 12:30 AM CDT two-hours-earlier reminder; a 4:30 AM CDT spring game
  has a 1:30 AM CST reminder.
- The existing minute heartbeat processes a bounded due-first/rotating batch.
  No additional scheduler job or unbounded queue. Whole-drain timeout is
  25 seconds; transport attempts also have a timeout.
- Identity includes the originating platform, source ID, event, player, sport,
  stat, original line, direction and classification. A readable legacy matchup
  audit label can differ from the verified provider event ID; reminder metadata
  retains both without rewriting the audit.
- A fresh current quote must still match the original selection, including
  source/event/classification, numeric line and selectable direction.
  Removal, started/invalid event, unavailable side, changed line, or confirmed
  absence from its complete source snapshot cancels the pending reminder.
  One provider route's missing quote does not invalidate another route's quote.
- Verified event rescheduling recomputes the reminder from the new event start
  without changing the original pick.
- Initial qualification inside two hours still sends the pick immediately;
  the persisted reminder is `elapsed`, reason `due_time_already_passed`.
- An atomic `pending` to `sending` claim prevents concurrent/restart replay.
  Reminder outcomes never count as ACTIONABLE_PICK deliveries or alter original
  opportunity qualification.
- Tests exercise scheduling, actual reminder dispatch through mocked Telegram
  transport, restart replay, at-most-once claims, cancellation, rescheduling,
  platform separation, immutability and simultaneous LINE CHANGE behavior.

### Explicit limitations

- The existing heartbeat provides minute-resolution dispatch, not hard
  real-time scheduling. Due timestamps are exact; dispatch may occur on the
  next tick. More than two minutes late is recorded as elapsed, not sent as
  a misleading two-hour reminder.
- A transport crash/timeout after a claim is uncertain. Its `sending` state
  is deliberately not retried, preferring a possibly missed reminder to a
  duplicate. Telegram does not provide an exactly-once transaction with SQLite.
- After restart, a fresh safely matched quote must arrive before a reminder
  can send. Old audits lacking a verified event timestamp are not backfilled
  with guessed times or armed retroactively.
- No live test messages or extra provider requests were issued for validation.
  Reminder network behavior was tested with mocked transport and isolated
  test databases, not asserted as a live Telegram reminder.

## Validation

- Final focused command:
  `python -m pytest bot/tests/test_v4_final_policy.py bot/tests/test_v4_pick_delivery_followup.py bot/tests/test_alert_quality.py bot/tests/test_strongest_pick.py bot/tests/test_live_gate_et_format.py bot/tests/test_pregame_opportunity_lifecycle.py bot/tests/test_alert_limits.py bot/tests/test_propline_cs_priority.py bot/tests/test_pregame_watch_delivery_gate.py bot/tests/test_testalert_pp_underdog.py bot/tests/test_telegram_reconciliation.py bot/tests/test_underdog_new_prop.py bot/tests/test_v4_market_offerings.py -q`
  — **466 passed, 40.71 seconds**, including **48 new policy tests**.
- Full suite: `python -m pytest bot/tests -q` — **5,478 passed in 420.90
  seconds**, no failures.
- `python -m compileall -q bot`: passed, exit 0.
- `git diff --check`: passed, exit 0.
- An earlier partial full-suite attempt was stopped to repair the demonstrated
  readable-audit-label/provider-event-ID mismatch. It is not counted as a pass.
- First completed full run: **5,434 passed, 44 failed**, 444.98 seconds.
  Failures demonstrated superseded history assumptions and transport mocks
  missing current-day timestamps, not a reason to bypass production safety.
  The legacy tests now isolate calendar policy from transport/identity tests,
  use six-game NFL fixtures, and use MLB rather than optional-history NHL for
  the unchanged proxy-evidence rejection tests. Subsequent affected-file check:
  156 passed, one additional failed-transport fixture still had five games and
  no date; that fixture was corrected. Final combined focused set passes above.
- Full-suite output is retained in ignored workspace file
  `.local/v4-final-policy-full.log`; the first completed run is preserved in
  `.local/v4-final-policy-full-first.log`.
- Final diff reviewed for scope: no provider fetching, grading, history-cache
  hydration, batch-serialization or quota implementation was replaced.
- Restarted **only Sharp Money Bot** once after the application changes.
  Runtime reported `running`, health port 8099, `Scheduler started` and
  `Application started`, with no startup ERROR lines or tracebacks.
  Exactly one `python bot/main.py` process was verified.
- Browser screenshot of port 8099 `/health` displayed **OK**. Its browser
  reported a separate 404 resource load; the health document itself loaded.
  This is process/HTTP health, not proof of a live reminder or provider result.
  Unrelated API and canvas services were not restarted.
- In a post-restart workflow-log sample, there were **64 Telegram alert-send
  failure entries**. The redacted exception summary did not identify a cause;
  this task did not send a test message or diagnose the bot/chat connection.
  The `/health` response and reminder-drain status do not establish that
  Telegram delivery is currently working.

## Exact changed files

### Application
1. `bot/alerts.py` — same-day terminal gates, shared Central display and
   immutable verified reminder metadata on both delivery paths.
2. `bot/alerts_multiplatform.py` — replace hardcoded Eastern display with
   shared Central conversion; compatibility function name retained.
3. `bot/config.py` — explicit America/Chicago reporting configuration.
4. `bot/reporting.py` — consistent Central fallback and UTC-naive `now` handling.
5. `bot/engine/delivery_evidence.py` — MLB/NFL six-game split; other sports optional.
6. `bot/engine/strongest_pick.py` — apply that same rule after evaluation.
7. `bot/engine/ud_bet_decision.py` — optional sports may derive a supported
   direction from short actual history, with existing rate thresholds.
8. `bot/engine/game_reminders.py` — new bounded durable reminder lifecycle.
9. `bot/database.py` — atomic PLAY/reminder creation and bounded reminder
   query/claim/state methods using existing columns.
10. `bot/engine/line_change_notifications.py` — share completed observations
    with reminder validation without coupling delivery/deduplication.
11. `bot/market_engine.py` — source-scoped observation handoff, cache reset,
    and six-game qualification integration; existing feed scheduling unchanged.
12. `bot/main.py` — bounded reminder drain in the existing heartbeat.

### Tests
13. `bot/tests/test_v4_final_policy.py` — 48 new controlled policy regressions.
14. `bot/tests/test_v4_pick_delivery_followup.py` — replace superseded next-day
    delivery expectation with explicit blocking.
15. `bot/tests/test_alert_limits.py` — isolate calendar checks in legacy cap
    mocks and verify tomorrow blocking.
16. `bot/tests/test_alert_quality.py` — updated sport history policy.
17. `bot/tests/test_strongest_pick.py` — updated six-game/optional-history policy.
18. `bot/tests/test_live_gate_et_format.py` — Central display/fallback expectations.
19. `bot/tests/test_pregame_opportunity_lifecycle.py` — restored next-day block.
20. `bot/tests/test_propline_cs_priority.py` — six-game NFL fixtures and optional
    NBA/WNBA history expectations.

21. `bot/tests/test_pregame_watch_delivery_gate.py` — NFL is the
    historical-evidence-required example, rather than optional-history NHL.
22. `bot/tests/test_testalert_pp_underdog.py` — isolate legacy transport mock
    timestamps and provide six actual games in NFL delivery fixtures.
23. `bot/tests/test_telegram_reconciliation.py` — preserve exact source/audit
    assertions with six-game evidence and calendar-isolated transport mocks.
24. `bot/tests/test_underdog_new_prop.py` — test MLB's retained rejection of
    proxy-only validation rather than imposing that requirement on NHL.
25. `bot/tests/test_v4_market_offerings.py` — isolate selectable-side safety
    from deliberately tomorrow-dated market inventory fixtures.

### Policy notes and report
26. `.agents/memory/MEMORY.md`
27. `.agents/memory/pregame-safety-boundary.md`
28. `.agents/memory/strong-pick-quality-gate.md`
29. `.agents/memory/two-layer-alert-architecture.md`
30. `.agents/memory/market-signal-bypass.md`
31. `docs/v4-final-policy-verification.md`

No GitHub push, Railway deployment, destructive backfill, database reset,
manual production mutation, provider addition or The Odds API activation.
