<!-- Source URL: https://developers.pandascore.co/docs/matches-lifecycle.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2025-09-16T00:29:12.000Z
---

Fetch the complete documentation index at: https://developers.pandascore.co/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Matches lifecycle

Learn how PandaScore REST API handles matches life cycle, from start to finish.

PandaScore implements match statuses as a finite state machine. The diagram below is a recap of all existing match statuses (the states) and the events that can affect them (the transitions).

<Image alt="The match status state machine" align="center" src="https://files.readme.io/c4a0179-statuses.PNG">
  The match status state machine
</Image>

## Not started

Matches always start with the `not_started` status. In this state:

* `status` is `not_started`
* `scheduled_at` is the official match playing time — as announced by the organizer
* `begin_at` has the same value as `scheduled_at` (to support some legacy integrations)
* `end_at` is `null`

For matches that support real-time in-game data, the **WebSocket opens 15 minutes before the scheduled time**.

> Retrieve the list of open WebSockets with the [All video games > List live matches](https://developers.pandascore.co/reference/get_lives) endpoint.

### Rescheduled matches

When an organizer decides to reschedule match, the `rescheduled` field will be set to `true`. In this state:

* `rescheduled` is `true`
* `scheduled_at` is the new official playing time — as announced by the organizer
* `original_scheduled_at` is the initial official playing time

<Image alt="An example of a match which is rescheduled from date_1 to date_2" align="center" src="https://files.readme.io/973f9f1-rescheduled.PNG">
  An example of a match which is rescheduled from date\_1 to date\_2
</Image>

> 🚧 Warning
>
> Matches will only be marked as rescheduled when the organizer officially announces a new date. Matches that are delayed (e.g. because the previous match is not finished) will *not* be marked as rescheduled.

## Running

Match status is updated to `running` as soon as the match begins. In this state:

* `status` is `running`
* `begin_at` is set to the actual beginning time of the match

<Image alt="An example of a match which begins at date_2, transitioning to the running status" align="center" src="https://files.readme.io/92d1eae-running.PNG">
  An example of a match which begins at date\_2, transitioning to the running status
</Image>

## Finished

Match status is updated to `finished` as soon as the match ends. In this state:

* `status` is `finished`
* `end_at` is set to the match finish time
* `winner_id` is updated to the winning team/player id

### Complete games

For matches that support historical data (i.e. `detailed_stats` is `true`) the 'complete' field indicates whether post-game statistics are whole and available for user consumption. Once a match is complete, the data should not change. In this state:

* `status` is `finished`
* `complete` is `true`

<Image alt="An example of a match which is complete, meaning all statistics are available" align="center" src="https://files.readme.io/8482067-complete.PNG">
  An example of a match which is complete, meaning all statistics are available
</Image>

## Canceled

Sometimes, matches can be canceled. Reasons may vary and different scenarios exist: cancellation by the organizer or one participant forfeiting.

### Without winner

In this scenario, the match status is updated to `canceled`. In this state:

* `status` is `canceled`
* `forfeit` is `false`
* `begin_at` is `null`
* `end_at` is `null`

A match enters the `canceled` status when, for various potential reasons, it won't be played.

### Forfeit

In this scenario too, the match status is updated to `canceled`. However, in this state:

* `status` is `canceled`
* `forfeit` is `true`
* `begin_at` is `null`
* `end_at` is `null`
* `winner_id` is updated to the winning team/player `id`

<Image alt="An example of a match which has been forfeited by team_2" align="center" src="https://files.readme.io/f14b896-forfeit.PNG">
  An example of a match which has been forfeited by team\_2
</Image>

## Postponed

Match status is updated to `postponed` when the match is rescheduled to an unknown date. In this state:

* `status` is `postponed`
* `scheduled_at` is not updated

When a new playing date is known, postponed matches go through the same procedure as [rescheduled matches](https://developers.pandascore.co/docs/matches-lifecycle#rescheduled-matches).

**Example**

<Image alt="An example of a postponed match which is then rescheduled from date_1 to date_2" align="center" src="https://files.readme.io/68db417-postponed.PNG">
  An example of a postponed match which is then rescheduled from date\_1 to date\_2
</Image>